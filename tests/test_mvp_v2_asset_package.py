from __future__ import annotations

import json
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as SpreadsheetImage
from PIL import Image

from mvp_v2.auto_pipeline import (
    AutoPipelineMediaManifest,
    analyze_auto_pipeline_media_manifest,
)
from mvp_v2.asset_package import (
    AssetPackageError,
    asset_package_index_summary,
    asset_package_source_record,
    bundle_asset_package_v2,
    bundle_asset_package_v3,
    create_asset_package,
    load_asset_package,
    package_media_for_brief,
    package_script_catalog,
    search_asset_package,
    update_asset_package_index,
)
from mvp_v2.generation_stages import _bindings
from mvp_v2.evolution import summarize_evolution_ledgers
from mvp_v2.user_feedback import (
    find_child_revision_constraints,
    load_feedback_status,
    load_revision_constraints,
    record_feedback_status,
    store_user_feedback,
)
from mvp_v2.loop_runner import (
    _can_reuse_stage_output,
    _can_reuse_upstream_for_image_feedback,
    _constraints_for_drafting,
    _constraints_for_planning,
    _has_targeted_non_visual_feedback,
    _preserve_unaffected_parent_visuals,
    _visual_preferences_from_feedback,
)
from mvp_v2.models import SourceShot
from mvp_v2.run_contract import sha256_file
from mvp_v2.template_rules import builtin_template_rules
from mvp_v2.pipeline_stages import _merge_manifest_rows
from mvp_v2.image_retrieval import search_images_for_slot
from mvp_v2.visual_assets import (
    _best_manifest_candidate,
    _strict_primary_visual_missing,
    apply_visual_analysis,
    attach_visual_assets,
    hydrate_visual_asset_identities,
    rematch_visual_assets,
)
from mvp_v2.visual_candidates import candidates_for_run


class AssetPackageTests(unittest.TestCase):
    def test_bundle_v2_is_self_contained_and_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            workbook_path = source / "M+发布会类通用脚本库.xlsx"
            _write_source_with_reference_image(workbook_path, root / "reference.png")
            v1 = create_asset_package(
                root / "v1",
                package_id="launch-core",
                source_root=source,
            )

            v2 = bundle_asset_package_v2(v1, root / "v2" / "1")
            source.rename(root / "source-detached")
            package = load_asset_package(v2)
            source_record = asset_package_source_record(v2)
            template, _, _ = package_script_catalog(
                v2,
                expected_category_id="launch",
                source_view="human",
            )
            manifest_rows, _ = package_media_for_brief(
                v2,
                {"项目名称": "测试发布会", "脚本类型": "发布会"},
            )

            self.assertEqual(package["schema_version"], "mplus-asset-package/v2")
            self.assertTrue(Path(source_record["source_workbook_path"]).is_file())
            self.assertEqual(len(manifest_rows), 1)
            self.assertTrue(Path(manifest_rows[0]["原图路径"]).is_file())
            self.assertTrue(
                Path(manifest_rows[0]["原图路径"]).is_relative_to(package["package_root"])
            )
            self.assertTrue(Path(template[0].reference_image_path).is_file())
            self.assertTrue(Path(template[0].reference_image_path).is_relative_to(package["package_root"]))
            with self.assertRaisesRegex(AssetPackageError, "不可变"):
                update_asset_package_index(v2)

    def test_bundle_v3_can_publish_new_version_from_immutable_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            workbook_path = source / "M+发布会类通用脚本库.xlsx"
            _write_source_with_reference_image(workbook_path, root / "reference.png")
            v1 = create_asset_package(
                root / "v1", package_id="launch-core", source_root=source,
            )
            v2 = bundle_asset_package_v2(v1, root / "versions" / "3", package_version=3)
            v3 = bundle_asset_package_v3(v2, root / "versions" / "4", package_version=4)
            package = load_asset_package(v3)

            self.assertEqual(package["schema_version"], "mplus-asset-package/v3")
            self.assertEqual(package["package_version"], 4)
            self.assertEqual(package["template_rule_contract"]["status"], "ready")
            self.assertTrue((package["package_root"] / "template_rules.json").is_file())
            self.assertTrue((root / "versions" / "3" / "asset_package.json").is_file())

    def test_bundle_v3_rebinds_an_immutable_v3_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source_with_reference_image(source / "M+发布会类通用脚本库.xlsx", root / "reference.png")
            v1 = create_asset_package(root / "v1", package_id="launch-core", source_root=source)
            v2 = bundle_asset_package_v2(v1, root / "versions" / "3", package_version=3)
            v3 = bundle_asset_package_v3(v2, root / "versions" / "4", package_version=4)
            original_manifest = (root / "versions" / "4" / "asset_package.json").read_bytes()

            v5 = bundle_asset_package_v3(v3, root / "versions" / "5", package_version=5)
            rebound = load_asset_package(v5)

            self.assertEqual(rebound["schema_version"], "mplus-asset-package/v3")
            self.assertEqual(rebound["package_version"], 5)
            self.assertEqual((root / "versions" / "4" / "asset_package.json").read_bytes(), original_manifest)
            self.assertEqual(asset_package_source_record(v5)["package_id"], "launch-core")

    def test_bundle_v3_preserves_bound_custom_rules_and_source_bytes(self):
        for relative_rules in ("template_rules.json", "rules/custom-structure.json"):
            with self.subTest(relative_rules=relative_rules), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                source.mkdir()
                _write_source(source / "M+发布会类通用脚本库.xlsx")
                v1 = create_asset_package(root / "v1", package_id="launch-core", source_root=source)
                v3 = bundle_asset_package_v3(v1, root / "versions" / "3", package_version=3)
                manifest = json.loads(v3.read_text(encoding="utf-8"))
                rules = builtin_template_rules("launch")
                rules["rules"][0]["value"] = "产品体验先行"
                rules_path = v3.parent / relative_rules
                rules_path.parent.mkdir(parents=True, exist_ok=True)
                rules_path.write_text(json.dumps(rules, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
                manifest["template_rules"] = relative_rules
                manifest["template_rules_sha256"] = sha256_file(rules_path)
                v3.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                original = load_asset_package(v3)
                self.assertEqual(original["template_rule_contract"]["status"], "ready")
                original_bytes = {
                    path.relative_to(v3.parent): path.read_bytes()
                    for path in v3.parent.rglob("*") if path.is_file()
                }

                upgraded = bundle_asset_package_v3(v3, root / "versions" / "4", package_version=4)
                package = load_asset_package(upgraded)

                self.assertEqual(package["template_rule_contract"]["payload"], rules)
                self.assertEqual(package["template_rule_contract"]["status"], "ready")
                self.assertEqual(package["template_rules"], relative_rules)
                self.assertEqual(package["template_rules_sha256"], manifest["template_rules_sha256"])
                self.assertEqual((upgraded.parent / relative_rules).read_bytes(), original_bytes[Path(relative_rules)])
                self.assertEqual(package["package_version"], 4)
                self.assertEqual(asset_package_source_record(upgraded)["package_id"], "launch-core")
                self.assertEqual({
                    path.relative_to(v3.parent): path.read_bytes()
                    for path in v3.parent.rglob("*") if path.is_file()
                }, original_bytes)

    def test_bundle_v3_keeps_builtin_rules_for_legacy_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            v1 = create_asset_package(root / "v1", package_id="launch-core", source_root=source)
            v2 = bundle_asset_package_v2(v1, root / "v2", package_version=2)
            for name, original in (("v1", v1), ("v2", v2)):
                with self.subTest(source_schema=name):
                    original_manifest = original.read_bytes()
                    upgraded = bundle_asset_package_v3(original, root / f"from-{name}", package_version=3)
                    package = load_asset_package(upgraded)
                    self.assertEqual(package["template_rule_contract"]["status"], "ready")
                    self.assertEqual(package["template_rule_contract"]["payload"], builtin_template_rules("launch"))
                    self.assertEqual(package["template_rules_sha256"], sha256_file(upgraded.parent / "template_rules.json"))
                    self.assertEqual(original.read_bytes(), original_manifest)

    def test_non_launch_specific_visuals_require_all_named_subjects(self):
        self.assertEqual(
            _strict_primary_visual_missing(
                "参赛者在赛车模拟器操作方向盘",
                "女性运动员在霓虹空间攀爬金属梯级",
                ["运动员", "健身", "霓虹灯"],
            ),
            ["赛车/车辆"],
        )
        self.assertEqual(
            _strict_primary_visual_missing(
                "赛事主题海报与挑战者完成轮胎互动",
                "夜市人群面对镜头互动",
                ["夜市", "人群", "互动"],
            ),
            ["主视觉/KV/END", "轮胎"],
        )
        self.assertEqual(
            _strict_primary_visual_missing(
                "俯拍赛车赛道，参与者完成轮胎互动挑战",
                "商场中庭赛车跑道，儿童使用工具击打轮胎",
                ["赛车", "跑道", "儿童互动", "轮胎"],
            ),
            [],
        )

    def test_supplemental_duplicate_keeps_indexed_media_identity(self):
        indexed = [{
            "image_id": "media_stable",
            "原图路径": "/tmp/reference.png",
            "analysis_required": False,
        }]
        supplemental = [{
            "image_id": "img_path_based",
            "原图路径": "/tmp/reference.png",
            "analysis_required": True,
        }]

        merged = _merge_manifest_rows(supplemental, indexed)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["image_id"], "media_stable")
        self.assertFalse(merged[0]["analysis_required"])

    def test_unanalyzed_source_reference_is_indexed_without_brand_hint_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            workbook_path = source / "M+发布会类通用脚本库.xlsx"
            _write_source_with_reference_image(workbook_path, root / "reference.png")
            package = create_asset_package(
                root / "package",
                package_id="launch-core",
                source_root=source,
            )

            manifest, analysis = package_media_for_brief(
                package,
                {"项目名称": "全新品牌发布会", "品牌或客户": "全新品牌"},
            )

        self.assertEqual(len(manifest), 1)
        self.assertEqual(analysis, [])
        self.assertEqual(manifest[0]["asset_role"], "source_reference")
        self.assertFalse(manifest[0]["analysis_required"])
        self.assertEqual(manifest[0]["semantic_basis"], "source_row")
        self.assertEqual(manifest[0]["来源 sheet"], "模版")
        self.assertEqual(manifest[0]["来源行"], 7)
        self.assertTrue(manifest[0]["image_id"].startswith("media_"))

    def test_source_reference_identity_hydrates_without_visual_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "reference.png"
            Image.new("RGB", (1280, 720), "white").save(image_path)
            scripts = [{"镜号": "1", "画面示意": str(image_path), "画面描述": "参考画面"}]
            details = [{
                "slot_index": 1,
                "模板镜号": "1",
                "模板来源文件": "已审核源表.xlsx",
                "模板来源 sheet": "模版",
                "模板来源行": 7,
            }]
            details, assets = attach_visual_assets(scripts, details)

            details, assets = hydrate_visual_asset_identities(details, assets, [{
                "image_id": "media_stable",
                "原图路径": str(Path(tmp) / "canonical.png"),
                "alternate_paths": [str(image_path)],
                "asset_role": "source_reference",
                "semantic_basis": "source_row",
            }])

        self.assertEqual(assets[0]["image_id"], "media_stable")
        self.assertEqual(assets[0]["视觉读取状态"], "未读取")
        self.assertEqual(details[0]["image_id"], "media_stable")
        self.assertEqual(assets[0]["索引语义依据"], "source_row")

    def test_source_row_semantics_are_manual_candidates_without_visual_claim(self):
        result = search_images_for_slot(
            {
                "slot_index": 11,
                "画面描述": "艺人登台亮相并与观众互动",
                "采用来源文件": "明星活动库.xlsx",
                "采用来源 sheet": "创意库",
                "采用来源行": 12,
            },
            {
                "项目名称": "新品牌明星活动",
                "品牌或客户": "新品牌",
                "脚本类型": "明星活动",
            },
            [{
                "image_id": "media_source_row",
                "原图路径": "/tmp/source-row.png",
                "预览路径": "/tmp/source-row.png",
                "asset_role": "source_reference",
                "source_semantic_text": "艺人登台亮相 观众互动 人物中景",
                "source_links": [{
                    "source_file": "明星活动库.xlsx",
                    "source_sheet": "创意库",
                    "source_row": 12,
                    "structure": "艺人亮相",
                    "shot_function": "艺人亮相",
                    "visual_subject": "艺人与观众",
                    "tags": "人物 互动",
                    "source_brand": "已审核案例品牌",
                }],
            }],
            [],
        )

        self.assertEqual(result["status"], "review")
        self.assertEqual(result["candidates"][0]["image_id"], "media_source_row")
        self.assertEqual(result["candidates"][0]["evidence_kind"], "source_row")
        self.assertTrue(result["candidates"][0]["requires_human_review"])
        self.assertLess(result["candidates"][0]["score"], 80)

    def test_analyzed_source_reference_with_other_brand_is_auto_function_reference(self):
        asset = {
            "image_id": "media_source_brand",
            "原图路径": "/tmp/source-brand.png",
            "预览路径": "/tmp/source-brand.png",
            "asset_role": "source_reference",
            "source_links": [{
                "source_file": "明星活动库.xlsx",
                "source_sheet": "模版",
                "source_row": 14,
                "structure": "人气造势",
                "shot_function": "人气规模展示",
                "visual_subject": "源品牌艺人与现场观众",
                "tags": "商场中庭 人群 艺人海报",
                "source_brand": "源品牌",
            }],
        }
        analysis = {
            "image_id": "media_source_brand",
            "caption": "源品牌在商场中庭举办明星活动，楼层围满观众",
            "tags": ["商场中庭", "明星活动", "人群聚集"],
            "brand_names": ["源品牌"],
            "ocr_text": "源品牌",
            "source": "fixture",
        }

        result = search_images_for_slot(
            {
                "slot_index": 8,
                "画面描述": "商场中庭人群聚集，展示活动人气规模",
                "镜头功能": "人气规模展示",
                "采用来源文件": "明星活动库.xlsx",
                "采用来源 sheet": "模版",
                "采用来源行": 14,
            },
            {"项目名称": "新品牌活动", "品牌或客户": "新品牌", "脚本类型": "明星活动"},
            [asset],
            [analysis],
        )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(result["brand_review_count"], 1)
        self.assertEqual(result["candidates"][0]["evidence_kind"], "visual_analysis")
        self.assertTrue(result["candidates"][0]["requires_human_review"])
        self.assertIn("源品牌", result["candidates"][0]["brand_warning"])
        self.assertGreaterEqual(result["candidates"][0]["score"], 80)
        self.assertEqual(result["candidates"][0]["judgement"], "功能参考")

    def test_reference_theme_stays_in_evidence_not_customer_launch_copy(self):
        from copy import deepcopy
        from mvp_v2.generator import generate_template_rows
        from mvp_v2.models import TemplateDecision
        from tests.test_mvp_v2_generic_brand_policy import _shot

        brief = {"项目名称": "新品牌星海计划发布会", "品牌或客户": "新品牌",
                 "脚本类型": "发布会", "视频时长": "60s"}
        source = _shot("创意库", "源品牌远山计划主题主视觉与会场人群", source_brand="源品牌")
        slot = _shot("模版", "开场主视觉", source_brand="新品牌")
        source_before = deepcopy(source)
        rows, details = generate_template_rows(brief,
            [TemplateDecision(slot, source, "REPLACE", 90, "仅参考镜头功能")])
        self.assertNotIn("远山计划", rows[0]["画面描述"])
        self.assertIn("星海计划", rows[0]["画面描述"])
        self.assertEqual(source, source_before)
        self.assertEqual(details[0]["采用来源文件"], source.source_file)
        self.assertEqual(details[0]["采用来源行"], source.source_row)
        asset = {"image_id": "anonymous-theme-reference", "原图路径": "/fixture/theme.png",
                 "asset_role": "source_reference", "source_links": [{
                     "source_file": source.source_file, "source_sheet": source.source_sheet,
                     "source_row": source.source_row, "source_brand": "源品牌",
                     "shot_function": "品牌开场", "tags": "品牌logo 主视觉"}]}
        analysis = {"image_id": asset["image_id"],
                    "caption": "源品牌远山计划主题主视觉，品牌logo入画",
                    "tags": ["品牌logo", "主视觉"], "brand_names": ["源品牌"],
                    "ocr_text": "远山计划", "source": "anonymous-fixture"}
        original = deepcopy((brief, asset, analysis, rows))
        preview = search_images_for_slot({"slot_index": 1,
            "画面描述": rows[0]["画面描述"], "镜头功能": "品牌开场",
            "采用来源文件": source.source_file, "采用来源 sheet": source.source_sheet,
            "采用来源行": source.source_row}, brief, [asset], [analysis])
        self.assertEqual(len(preview["candidates"]), 1, preview)
        candidate = preview["candidates"][0]
        self.assertEqual(candidate["summary"], analysis["caption"])
        self.assertEqual(candidate["ocr_text"], "远山计划")
        self.assertEqual(candidate["judgement"], "功能参考")
        self.assertTrue(candidate["requires_human_review"])
        self.assertIn("源品牌", candidate["brand_warning"])
        self.assertEqual((brief, asset, analysis, rows), original)

    def test_analyzed_external_media_with_other_brand_is_still_rejected(self):
        result = search_images_for_slot(
            {"slot_index": 1, "画面描述": "新品牌活动现场", "镜头功能": "会场建立"},
            {"项目名称": "新品牌活动", "品牌或客户": "新品牌", "脚本类型": "明星活动"},
            [{
                "image_id": "media_external_brand",
                "原图路径": "/tmp/external-brand.png",
                "asset_role": "external_media",
            }],
            [{
                "image_id": "media_external_brand",
                "caption": "源品牌活动现场",
                "tags": ["活动现场"],
                "brand_names": ["源品牌"],
                "ocr_text": "源品牌",
                "source": "fixture",
            }],
        )

        self.assertFalse(result["candidates"])
        self.assertTrue(any("其他品牌" in item["reason"] for item in result["rejected"]))

    def test_low_resolution_source_reference_can_be_kept_with_warning_semantics(self):
        result = search_images_for_slot(
            {
                "slot_index": 13,
                "画面描述": "赛事观众为选手加油，建立现场人气",
                "镜头功能": "观众反应",
                "采用来源文件": "赛事库.xlsx",
                "采用来源 sheet": "创意库",
                "采用来源行": 18,
            },
            {"项目名称": "新赛事", "品牌或客户": "新品牌", "脚本类型": "赛事"},
            [{
                "image_id": "media_low_reference",
                "原图路径": "/tmp/low-reference.png",
                "asset_role": "source_reference",
                "是否低清": True,
                "来源文件": "赛事库.xlsx",
                "来源 sheet": "创意库",
                "来源行": 18,
                "source_links": [{
                    "source_file": "赛事库.xlsx",
                    "source_sheet": "创意库",
                    "source_row": 18,
                }],
            }],
            [{
                "image_id": "media_low_reference",
                "caption": "源品牌赛事现场，观众在人群中为选手加油",
                "tags": ["赛事", "观众", "人群", "加油"],
                "brand_names": ["源品牌"],
                "ocr_text": "源品牌",
                "negative_flags": [],
                "source": "fixture",
            }],
        )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(result["candidates"][0]["judgement"], "功能参考")
        self.assertTrue(result["candidates"][0]["requires_human_review"])

    def test_non_launch_candidate_does_not_inherit_launch_product_conflict(self):
        result = search_images_for_slot(
            {
                "slot_index": 7,
                "画面描述": "轮胎挑战互动的手部与器材细节",
                "镜头功能": "装置互动体验",
            },
            {"项目名称": "城市挑战赛", "品牌或客户": "新品牌", "脚本类型": "赛事"},
            [{
                "image_id": "media_sports_interaction",
                "原图路径": "/tmp/sports-interaction.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "media_sports_interaction",
                "caption": "参赛者使用工具完成汽车轮胎互动挑战",
                "tags": ["汽车", "轮胎", "互动体验"],
                "brand_names": ["无"],
                "ocr_text": "",
                "source": "fixture",
            }],
        )

        self.assertTrue(result["candidates"])
        self.assertNotIn(
            "需要家电互动但画面是车机/汽车",
            result["candidates"][0]["match_reason"],
        )

    def test_non_launch_visual_semantics_can_outrank_source_row_binding(self):
        slot = {
            "slot_index": 9,
            "画面描述": "主舞台电竞舱内选手比赛，观众席满座",
            "镜头功能": "电竞对抗与赛事规模",
            "采用来源文件": "赛事库.xlsx",
            "采用来源 sheet": "创意库",
            "采用来源行": 9,
        }
        manifest = [{
            "image_id": "media_marathon",
            "原图路径": "/tmp/marathon.png",
            "asset_role": "source_reference",
            "来源文件": "赛事库.xlsx",
            "来源 sheet": "创意库",
            "来源行": 9,
            "source_links": [{
                "source_file": "赛事库.xlsx",
                "source_sheet": "创意库",
                "source_row": 9,
            }],
        }, {
            "image_id": "media_esports",
            "原图路径": "/tmp/esports.png",
            "asset_role": "source_reference",
            "来源文件": "赛事库.xlsx",
            "来源 sheet": "创意库",
            "来源行": 40,
        }]
        analysis = [{
            "image_id": "media_marathon",
            "caption": "城市马拉松起点，大批跑者准备起跑",
            "tags": ["马拉松", "跑者", "起跑"],
            "brand_names": ["源品牌"],
            "ocr_text": "源品牌",
            "source": "fixture",
        }, {
            "image_id": "media_esports",
            "caption": "电竞比赛主舞台，选手对抗，观众席满座",
            "tags": ["电竞", "比赛", "舞台", "选手", "观众"],
            "brand_names": ["源品牌"],
            "ocr_text": "源品牌",
            "source": "fixture",
        }]

        result = search_images_for_slot(
            slot,
            {"项目名称": "电竞总决赛", "品牌或客户": "新品牌", "脚本类型": "赛事"},
            manifest,
            analysis,
        )

        self.assertEqual(result["candidates"][0]["image_id"], "media_esports")
        self.assertGreaterEqual(result["candidates"][0]["score"], 80)
        self.assertTrue(any(
            item["image_id"] == "media_marathon"
            and "项目领域为电竞" in item["reason"]
            for item in result["rejected"]
        ))

    def test_background_only_audience_cannot_beat_audience_as_main_subject(self):
        slot = {
            "slot_index": 19,
            "画面描述": "手持穿梭观众区，应援棒与灯牌挥舞，众人齐声欢呼",
            "镜头功能": "观众应援盛况",
            "采用来源文件": "赛事库.xlsx",
            "采用来源 sheet": "模版",
            "采用来源行": 12,
        }
        manifest = [{
            "image_id": "media_beer",
            "原图路径": "/tmp/beer.png",
            "asset_role": "source_reference",
            "来源文件": "赛事库.xlsx",
            "来源 sheet": "模版",
            "来源行": 12,
        }, {
            "image_id": "media_crowd",
            "原图路径": "/tmp/crowd.png",
            "asset_role": "source_reference",
            "来源文件": "赛事库.xlsx",
            "来源 sheet": "创意库",
            "来源行": 45,
        }]
        analysis = [{
            "image_id": "media_beer",
            "caption": "特写展示桌上四瓶啤酒，背景为虚化的观众席，众多观众观看活动",
            "tags": ["啤酒", "产品展示", "活动现场"],
            "brand_names": ["源品牌"],
            "ocr_text": "源品牌",
            "source": "fixture",
        }, {
            "image_id": "media_crowd",
            "caption": "夜间活动中大批观众高举手机与应援棒欢呼",
            "tags": ["观众", "人群", "欢呼", "应援"],
            "brand_names": ["源品牌"],
            "ocr_text": "源品牌",
            "source": "fixture",
        }]

        result = search_images_for_slot(
            slot,
            {"项目名称": "电竞总决赛", "品牌或客户": "新品牌", "脚本类型": "赛事"},
            manifest,
            analysis,
        )

        self.assertEqual(result["candidates"][0]["image_id"], "media_crowd")
        self.assertTrue(any(
            "主体要求“人群/观众”仅出现在背景" in item["reason"]
            for item in result["rejected"]
            if item["image_id"] == "media_beer"
        ))

    def test_background_interaction_does_not_hide_relevant_foreground_mascot(self):
        result = search_images_for_slot(
            {
                "slot_index": 15,
                "画面描述": "前推赛事炫光入口，切换赛事吉祥物与粉丝击掌巡游",
                "镜头功能": "赛事IP互动",
                "采用来源文件": "赛事库.xlsx",
                "采用来源 sheet": "模版",
                "采用来源行": 24,
            },
            {"项目名称": "电竞总决赛", "品牌或客户": "新品牌", "脚本类型": "赛事"},
            [{
                "image_id": "media_generic_interaction",
                "原图路径": "/tmp/generic-interaction.png",
                "asset_role": "source_reference",
                "来源文件": "赛事库.xlsx",
                "来源 sheet": "创意库",
                "来源行": 24,
            }, {
                "image_id": "media_mascot",
                "原图路径": "/tmp/mascot.png",
                "asset_role": "source_reference",
                "来源文件": "赛事库.xlsx",
                "来源 sheet": "模版",
                "来源行": 24,
            }],
            [{
                "image_id": "media_generic_interaction",
                "caption": "户外健身活动现场，参与者在红色主题展台前互动，背景为现代建筑",
                "tags": ["赛事", "入口", "互动", "户外活动"],
                "brand_names": ["源品牌"],
                "ocr_text": "源品牌",
                "source": "fixture",
            }, {
                "image_id": "media_mascot",
                "caption": "电竞赛事入口前景是一名战队吉祥物，背景中有游客在展位周围参观互动",
                "tags": ["电竞", "赛事", "入口", "吉祥物", "互动"],
                "brand_names": ["源品牌"],
                "ocr_text": "源品牌",
                "source": "fixture",
            }],
        )

        self.assertEqual(result["candidates"][0]["image_id"], "media_mascot")
        self.assertFalse(any(
            item["image_id"] == "media_generic_interaction"
            for item in result["candidates"]
        ))
        self.assertTrue(any(
            item["image_id"] == "media_generic_interaction"
            and "缺少必须画面主体: IP/吉祥物" in item["reason"]
            for item in result["rejected"]
        ))
        self.assertNotIn("仅出现在背景", result["candidates"][0]["match_reason"])

    def test_cross_domain_mascot_does_not_bypass_event_domain_safety(self):
        result = search_images_for_slot(
            {
                "slot_index": 15,
                "画面描述": "前推夜跑赛事入口，切换赛事吉祥物与跑者击掌互动",
                "镜头功能": "赛事IP互动",
            },
            {
                "项目名称": "澄路城市夜跑接力赛",
                "脚本类型": "赛事",
                "行业或活动类别": "城市夜跑接力赛事",
            },
            [{
                "image_id": "media_racing_mascot",
                "原图路径": "/tmp/racing-mascot.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "media_racing_mascot",
                "caption": "F1赛车场入口前景是一名赛车服吉祥物与游客互动",
                "tags": ["F1", "赛车", "入口", "吉祥物", "互动"],
                "brand_names": ["无"],
                "source": "fixture",
            }],
        )

        self.assertFalse(result["candidates"])
        self.assertTrue(any(
            item["image_id"] == "media_racing_mascot"
            and "项目领域为跑步" in item["reason"]
            and "候选图明确属于赛车" in item["reason"]
            for item in result["rejected"]
        ))

    def test_baton_closeup_rejects_bottles_even_when_source_function_matches(self):
        result = search_images_for_slot(
            {
                "slot_index": 6,
                "画面描述": "特写印有赛事标识的荧光接力棒",
                "镜头功能": "品牌产品现场特写",
            },
            {
                "项目名称": "城市夜跑接力赛",
                "脚本类型": "赛事",
                "行业或活动类别": "城市夜跑接力赛事",
            },
            [{
                "image_id": "media_bottles",
                "原图路径": "/tmp/bottles.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "media_bottles",
                "caption": "户外车展人群前成排摆放啤酒瓶，背景为汽车展台",
                "tags": ["车展", "汽车", "啤酒瓶", "产品特写"],
                "brand_names": ["源品牌"],
                "source": "fixture",
            }],
        )

        self.assertFalse(result["candidates"])
        self.assertTrue(any(
            "缺少必须画面主体: 接力棒/火炬" in item["reason"]
            for item in result["rejected"]
        ))

    def test_sports_end_card_rejects_unrelated_idol_stage(self):
        result = search_images_for_slot(
            {
                "slot_index": 22,
                "画面描述": "城市夜跑接力赛主题海报居中定格，完成赛事收官落版",
                "镜头功能": "赛事主题收官落版",
            },
            {
                "项目名称": "城市夜跑接力赛",
                "脚本类型": "赛事",
                "行业或活动类别": "城市夜跑接力赛事",
            },
            [{
                "image_id": "media_idol_stage",
                "原图路径": "/tmp/idol-stage.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "media_idol_stage",
                "caption": "九位年轻艺人与两位主持人站在舞台上，背景展示成员个人海报",
                "tags": ["艺人", "主持人", "偶像团体", "舞台"],
                "brand_names": ["源品牌"],
                "source": "fixture",
            }],
        )

        self.assertFalse(result["candidates"])
        self.assertTrue(any(
            "赛事收官主视觉缺少当前活动领域依据" in item["reason"]
            for item in result["rejected"]
        ))

    def test_background_audience_is_allowed_when_requested_runner_is_foreground(self):
        result = search_images_for_slot(
            {
                "slot_index": 11,
                "画面描述": "跟拍接力跑者冲出起点，切换观众挥手呐喊",
                "镜头功能": "参赛者群像快切",
            },
            {"项目名称": "城市接力赛", "品牌或客户": "新品牌", "脚本类型": "赛事"},
            [{
                "image_id": "media_runner",
                "原图路径": "/tmp/runner.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "media_runner",
                "caption": "马拉松跑者在赛道冲刺，背景中可见模糊的观众人群",
                "tags": ["跑者", "比赛", "冲刺"],
                "brand_names": ["源品牌"],
                "ocr_text": "源品牌",
                "source": "fixture",
            }],
        )

        self.assertEqual(result["candidates"][0]["image_id"], "media_runner")
        self.assertFalse(result["candidates"][0]["conflicts"])

    def test_rematch_replaces_foreground_product_when_audience_is_only_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            beer = root / "beer.png"
            crowd = root / "crowd.png"
            Image.new("RGB", (1920, 1080), "green").save(beer)
            Image.new("RGB", (1920, 1080), "blue").save(crowd)
            script_rows = [{
                "镜号": "19",
                "结构": "高潮",
                "画面示意": str(beer),
                "画面描述": "手持穿梭观众区，应援棒与灯牌挥舞，众人齐声欢呼",
            }]
            detail_rows = [{
                "slot_index": 19,
                "逻辑槽位": 19,
                "模板镜号": "19",
                "槽位功能": "观众应援盛况",
                "模板来源文件": "赛事库.xlsx",
                "模板来源 sheet": "模版",
                "模板来源行": 12,
            }]
            detail_rows, assets = attach_visual_assets(script_rows, detail_rows)
            analysis_by_path = {
                str(beer): {
                    "summary": "特写展示桌上四瓶啤酒，背景为虚化的观众席，众多观众观看活动",
                    "tags": ["啤酒", "产品展示", "活动现场"],
                    "brand_names": ["源品牌"],
                    "source": "fixture",
                },
            }
            detail_rows, assets = apply_visual_analysis(
                script_rows, detail_rows, assets, analysis_by_path
            )
            manifest = [{
                "image_id": "media_beer",
                "原图路径": str(beer),
                "asset_role": "source_reference",
                "来源文件": "赛事库.xlsx",
                "来源 sheet": "模版",
                "来源行": 12,
                "是否低清": False,
            }, {
                "image_id": "media_crowd",
                "原图路径": str(crowd),
                "asset_role": "source_reference",
                "来源文件": "赛事库.xlsx",
                "来源 sheet": "创意库",
                "来源行": 45,
                "是否低清": False,
            }]
            analysis_rows = [{
                "image_id": "media_beer",
                "caption": "特写展示桌上四瓶啤酒，背景为虚化的观众席，众多观众观看活动",
                "tags": ["啤酒", "产品展示", "活动现场"],
                "brand_names": ["源品牌"],
                "ocr_text": "源品牌",
                "source": "fixture",
            }, {
                "image_id": "media_crowd",
                "caption": "夜间活动中大批观众高举手机与应援棒欢呼",
                "tags": ["观众", "人群", "欢呼", "应援"],
                "brand_names": ["源品牌"],
                "ocr_text": "源品牌",
                "source": "fixture",
            }]

            rows, details, rematched = rematch_visual_assets(
                script_rows,
                detail_rows,
                assets,
                {},
                [beer, crowd],
                manifest_rows=manifest,
                analysis_rows=analysis_rows,
                brief={"项目名称": "电竞总决赛", "品牌或客户": "新品牌", "脚本类型": "赛事"},
            )

        self.assertEqual(rows[0]["画面示意"], str(crowd))
        self.assertEqual(rematched[0]["image_id"], "media_crowd")
        self.assertIn("主体仅出现在背景", details[0]["图片绑定说明"])

    def test_non_launch_current_visual_does_not_inherit_launch_slot_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "mascot.png"
            Image.new("RGB", (1920, 1080), "red").save(image_path)
            script_rows = [{
                "镜号": "15",
                "画面示意": str(image_path),
                "画面描述": "前推赛事炫光入口，切换赛事吉祥物与粉丝击掌巡游",
            }]
            detail_rows = [{
                "slot_index": 15,
                "逻辑槽位": 15,
                "模板镜号": "15",
                "槽位功能": "赛事IP互动",
                "图片绑定说明": "已审核赛事功能参考",
            }]
            assets = [{
                "visual_asset_id": "va_mascot",
                "image_id": "media_mascot",
                "asset_role": "source_reference",
                "资产角色": "source_reference",
                "slot_index": 15,
                "镜号": "15",
                "原图路径": str(image_path),
                "质量指标": "高清原图",
                "视觉内容摘要": "电竞战队吉祥物站在赛事入口与粉丝互动",
                "视觉标签": "电竞；赛事；吉祥物；入口；互动",
                "视觉匹配分": 100,
                "配图判断": "匹配",
            }]

            rows, details, rematched = rematch_visual_assets(
                script_rows,
                detail_rows,
                assets,
                {},
                [],
                brief={"项目名称": "电竞总决赛", "脚本类型": "赛事"},
            )

        self.assertEqual(rows[0]["画面示意"], str(image_path))
        self.assertEqual(rematched[0]["image_id"], "media_mascot")
        self.assertNotIn("不符合槽位允许的镜头功能", details[0]["图片绑定说明"])

    def test_rematch_clears_explicit_cross_domain_source_reference_without_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "esports.png"
            Image.new("RGB", (1920, 1080), "blue").save(image_path)
            script_rows = [{
                "镜号": "17",
                "画面示意": str(image_path),
                "画面描述": "夜跑赛道旁观众举起手机为跑者应援",
            }]
            detail_rows = [{
                "slot_index": 17,
                "逻辑槽位": 17,
                "模板镜号": "17",
                "槽位功能": "观众应援盛况",
            }]
            assets = [{
                "visual_asset_id": "va_esports",
                "image_id": "media_esports",
                "asset_role": "source_reference",
                "资产角色": "source_reference",
                "slot_index": 17,
                "镜号": "17",
                "原图路径": str(image_path),
                "绑定方式": "行绑定",
                "绑定说明": "已审核源参考图",
                "质量指标": "高清原图",
                "视觉读取状态": "minimax",
                "配图置信度": "高",
                "视觉内容摘要": "电竞比赛现场，观众在舞台前为游戏选手欢呼",
                "视觉标签": "电竞；游戏比赛；观众",
                "视觉匹配分": 100,
                "配图判断": "功能参考",
            }]
            manifest = [{
                "image_id": "media_esports",
                "原图路径": str(image_path),
                "asset_role": "source_reference",
                "是否低清": False,
            }]
            analysis_rows = [{
                "image_id": "media_esports",
                "caption": "电竞比赛现场，观众在舞台前为游戏选手欢呼",
                "tags": ["电竞", "游戏比赛", "观众"],
                "brand_names": ["无"],
                "source": "fixture",
            }]

            rows, details, rematched = rematch_visual_assets(
                script_rows,
                detail_rows,
                assets,
                {},
                [image_path],
                manifest_rows=manifest,
                analysis_rows=analysis_rows,
                brief={"项目名称": "澄路夜跑接力赛", "脚本类型": "赛事"},
            )

        self.assertEqual(rows[0]["画面示意"], "")
        self.assertEqual(rematched[0]["image_id"], "")
        self.assertIn("项目领域为跑步", details[0]["图片绑定说明"])

    def test_equipment_shot_does_not_adopt_unrelated_product_reference(self):
        result = search_images_for_slot(
            {
                "slot_index": 6,
                "画面描述": "浅景深特写成排电竞外设与包装，冷光映衬",
                "镜头功能": "设备准备",
                "采用来源文件": "赛事库.xlsx",
                "采用来源 sheet": "模版",
                "采用来源行": 12,
            },
            {"项目名称": "电竞总决赛", "品牌或客户": "新品牌", "脚本类型": "赛事"},
            [{
                "image_id": "media_beer_product",
                "原图路径": "/tmp/beer-product.png",
                "asset_role": "source_reference",
                "来源文件": "赛事库.xlsx",
                "来源 sheet": "模版",
                "来源行": 12,
            }],
            [{
                "image_id": "media_beer_product",
                "caption": "特写展示桌上四瓶啤酒，背景是虚化的活动人群",
                "tags": ["啤酒", "瓶装", "产品展示"],
                "brand_names": ["源品牌"],
                "ocr_text": "源品牌",
                "source": "fixture",
            }],
        )

        self.assertFalse(result["candidates"])
        self.assertTrue(any(
            "缺少必须画面主体: 设备/外设" in item["reason"]
            for item in result["rejected"]
        ))

    def test_esports_slot_rejects_explicit_marathon_domain(self):
        result = search_images_for_slot(
            {
                "slot_index": 11,
                "画面描述": "手持跟拍选手专注侧脸，切观众席激动呐喊特写",
                "镜头功能": "选手与观众情绪",
            },
            {"项目名称": "镜域杯室内电竞总决赛", "脚本类型": "赛事"},
            [{
                "image_id": "media_marathon",
                "原图路径": "/tmp/marathon.png",
                "asset_role": "source_reference",
            }, {
                "image_id": "media_esports",
                "原图路径": "/tmp/esports.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "media_marathon",
                "caption": "马拉松跑者在户外赛道冲刺，背景中有观众人群",
                "tags": ["跑者", "马拉松", "观众"],
                "brand_names": ["无"],
                "source": "fixture",
            }, {
                "image_id": "media_esports",
                "caption": "电竞比赛现场，观众在舞台前观看选手比赛",
                "tags": ["电竞", "游戏选手", "观众"],
                "brand_names": ["无"],
                "source": "fixture",
            }],
        )

        self.assertEqual(result["candidates"][0]["image_id"], "media_esports")
        self.assertTrue(any(
            item["image_id"] == "media_marathon"
            and "项目领域为电竞" in item["reason"]
            for item in result["rejected"]
        ))

    def test_esports_domain_breaks_equal_score_tie_against_generic_event(self):
        result = search_images_for_slot(
            {
                "slot_index": 11,
                "画面描述": "选手专注侧脸，切观众席激动呐喊",
                "镜头功能": "选手与观众情绪",
            },
            {"项目名称": "镜域杯室内电竞总决赛", "脚本类型": "赛事"},
            [{
                "image_id": "media_generic",
                "原图路径": "/tmp/generic.png",
                "asset_role": "source_reference",
            }, {
                "image_id": "media_esports",
                "原图路径": "/tmp/esports.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "media_generic",
                "caption": "运动活动现场，选手与观众庆祝",
                "tags": ["选手", "观众"],
                "brand_names": ["无"],
                "source": "fixture",
            }, {
                "image_id": "media_esports",
                "caption": "电竞比赛现场，选手与观众庆祝",
                "tags": ["电竞", "选手", "观众"],
                "brand_names": ["无"],
                "source": "fixture",
            }],
        )

        self.assertEqual(result["candidates"][0]["image_id"], "media_esports")
        self.assertEqual(
            result["candidates"][0]["rank_components"]["domain_affinity"], 1
        )
        self.assertGreater(
            result["candidates"][0]["score"],
            result["candidates"][1]["score"],
        )

    def test_night_run_slot_rejects_explicit_esports_domain(self):
        result = search_images_for_slot(
            {
                "slot_index": 17,
                "画面描述": "夜跑赛道观众举起手机应援，与跑者互动",
                "镜头功能": "观众互动",
            },
            {"项目名称": "澄路夜跑接力赛", "脚本类型": "赛事"},
            [{
                "image_id": "media_esports",
                "原图路径": "/tmp/esports.png",
                "asset_role": "source_reference",
            }, {
                "image_id": "media_runner",
                "原图路径": "/tmp/runner.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "media_esports",
                "caption": "电竞比赛现场，观众举起手机庆祝获胜",
                "tags": ["电竞", "游戏选手", "观众"],
                "brand_names": ["无"],
                "source": "fixture",
            }, {
                "image_id": "media_runner",
                "caption": "夜跑赛道旁的观众举起手机为跑者应援",
                "tags": ["夜跑", "跑者", "观众", "手机"],
                "brand_names": ["无"],
                "source": "fixture",
            }],
        )

        self.assertEqual(result["candidates"][0]["image_id"], "media_runner")
        self.assertTrue(any(
            item["image_id"] == "media_esports"
            and "项目领域为跑步" in item["reason"]
            for item in result["rejected"]
        ))

    def test_non_launch_exterior_shot_rejects_indoor_esports_portrait(self):
        result = search_images_for_slot(
            {"slot_index": 1, "画面描述": "航拍室内电竞场馆外景，叠加赛事主标题"},
            {"项目名称": "镜域杯室内电竞总决赛", "脚本类型": "赛事"},
            [{
                "image_id": "media_portrait",
                "原图路径": "/tmp/portrait.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "media_portrait",
                "caption": "年轻男性在电竞椅前拍照，身后是选手比赛",
                "tags": ["电竞", "选手", "人物"],
                "brand_names": ["无"],
                "source": "fixture",
            }],
        )

        self.assertFalse(result["candidates"])
        self.assertTrue(any(
            "缺少必须画面主体: 外场/建筑" in item["reason"]
            for item in result["rejected"]
        ))

    def test_non_launch_city_opening_keeps_city_function_over_domain_only_image(self):
        result = search_images_for_slot(
            {
                "slot_index": 1,
                "画面描述": "航拍城市主干道与楼宇，叠加赛事活动信息",
                "镜头功能": "举办城市与活动信息建立",
                "采用来源文件": "赛事库.xlsx",
                "采用来源 sheet": "模版",
                "采用来源行": 7,
            },
            {
                "项目名称": "城市女子网球挑战赛",
                "脚本类型": "赛事",
                "行业或活动类别": "网球赛事",
            },
            [{
                "image_id": "media_city",
                "原图路径": "/tmp/city.png",
                "asset_role": "source_reference",
                "来源文件": "赛事库.xlsx",
                "来源 sheet": "模版",
                "来源行": 7,
            }, {
                "image_id": "media_tennis",
                "原图路径": "/tmp/tennis.png",
                "asset_role": "source_reference",
                "来源文件": "赛事库.xlsx",
                "来源 sheet": "创意库",
                "来源行": 28,
            }],
            [{
                "image_id": "media_city",
                "caption": "航拍城市CBD高楼与主干道，画面叠加活动标题",
                "tags": ["城市", "CBD", "高楼", "主干道"],
                "brand_names": ["无"],
                "source": "fixture",
            }, {
                "image_id": "media_tennis",
                "caption": "女子在网球场挥拍击球",
                "tags": ["网球", "挥拍", "运动员"],
                "brand_names": ["无"],
                "source": "fixture",
            }],
        )

        self.assertEqual(result["candidates"][0]["image_id"], "media_city")
        self.assertTrue(any(
            item["image_id"] == "media_tennis"
            and "缺少必须画面主体: 城市/楼宇" in item["reason"]
            for item in result["rejected"]
        ))

    def test_non_launch_required_tennis_action_promotes_matching_creative_image(self):
        result = search_images_for_slot(
            {
                "slot_index": 12,
                "画面描述": "三棱镜折射下，网球运动员挥拍击球",
                "镜头功能": "折射视角运动展示",
                "缺口说明": "Brief规划内容：网球运动员挥拍击球，须由已审核来源提供证据",
                "采用来源文件": "赛事库.xlsx",
                "采用来源 sheet": "模版",
                "采用来源行": 18,
            },
            {
                "项目名称": "城市女子网球挑战赛",
                "脚本类型": "赛事",
                "行业或活动类别": "网球赛事",
            },
            [{
                "image_id": "media_effect",
                "原图路径": "/tmp/effect.png",
                "asset_role": "source_reference",
                "来源文件": "赛事库.xlsx",
                "来源 sheet": "模版",
                "来源行": 18,
            }, {
                "image_id": "media_tennis",
                "原图路径": "/tmp/tennis.png",
                "asset_role": "source_reference",
                "来源文件": "赛事库.xlsx",
                "来源 sheet": "创意库",
                "来源行": 28,
                "source_links": [{
                    "structure": "竞技瞬间",
                    "shot_function": "球类竞技高光",
                    "visual_desc": "网球运动员挥拍击球",
                }],
            }],
            [{
                "image_id": "media_effect",
                "caption": "运动员在霓虹灯下攀爬金属梯级",
                "tags": ["运动员", "霓虹灯", "折射光效"],
                "brand_names": ["无"],
                "source": "fixture",
            }, {
                "image_id": "media_tennis",
                "caption": "一名女子在网球场发球，随后挥拍击球",
                "tags": ["网球", "运动员", "发球", "击球"],
                "brand_names": ["无"],
                "source": "fixture",
            }],
        )

        self.assertEqual(result["candidates"][0]["image_id"], "media_tennis")
        self.assertEqual(
            result["candidates"][0]["required_content_hits"],
            ["网球运动员挥拍击球"],
        )
        self.assertEqual(
            result["candidates"][0]["rank_components"]["required_content"],
            40,
        )

    def test_non_launch_beverage_slot_rejects_forbidden_drink_and_unrelated_domain_image(self):
        result = search_images_for_slot(
            {
                "slot_index": 16,
                "画面描述": "特写运动饮料注入杯中，再切两杯相碰",
                "镜头功能": "产品饮用与干杯",
            },
            {
                "项目名称": "城市女子网球挑战赛",
                "脚本类型": "赛事",
                "行业或活动类别": "网球赛事",
                "禁忌或不能出现的内容": "不出现啤酒",
            },
            [{
                "image_id": "media_beer",
                "原图路径": "/tmp/beer.png",
                "asset_role": "source_reference",
            }, {
                "image_id": "media_tennis",
                "原图路径": "/tmp/tennis.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "media_beer",
                "caption": "工作人员将啤酒倒入透明杯后举杯庆祝",
                "tags": ["啤酒", "倒酒", "碰杯"],
                "brand_names": ["无"],
                "source": "fixture",
            }, {
                "image_id": "media_tennis",
                "caption": "女子在网球场挥拍击球",
                "tags": ["网球", "挥拍"],
                "brand_names": ["无"],
                "source": "fixture",
            }],
        )

        self.assertFalse(result["candidates"])
        self.assertTrue(any(
            item["image_id"] == "media_beer"
            and "命中 Brief 禁止视觉: 啤酒" in item["reason"]
            for item in result["rejected"]
        ))
        self.assertTrue(any(
            item["image_id"] == "media_tennis"
            and "缺少必须画面主体: 饮料/杯" in item["reason"]
            for item in result["rejected"]
        ))

    def test_source_reference_unknown_brand_requires_review_but_external_is_rejected(self):
        analysis = {
            "caption": "宴会厅内长桌、花艺、酒杯与烛光布置",
            "tags": ["晚宴", "长桌", "花艺", "宴会厅"],
            "brand_names": ["未知"],
            "negative_flags": ["品牌不确定禁用"],
            "review_status": "blocked",
            "source": "fixture",
        }
        result = search_images_for_slot(
            {
                "slot_index": 1,
                "画面描述": "横移晚宴长桌、花艺与烛光",
                "镜头功能": "晚宴空间建立",
                "required_brief_content": ["晚宴长桌与花艺"],
            },
            {
                "项目名称": "测试企业年会",
                "脚本类型": "年会",
                "行业或活动类别": "室内周年晚宴",
            },
            [{
                "image_id": "source_unknown",
                "原图路径": "/tmp/source.png",
                "asset_role": "source_reference",
            }, {
                "image_id": "external_unknown",
                "原图路径": "/tmp/external.png",
                "asset_role": "external_media",
            }],
            [{"image_id": "source_unknown", **analysis}, {
                "image_id": "external_unknown", **analysis,
            }],
        )

        self.assertEqual(result["candidates"][0]["image_id"], "source_unknown")
        self.assertTrue(result["candidates"][0]["requires_human_review"])
        self.assertIn("品牌识别不确定，需人工确认", result["candidates"][0]["brand_warning"])
        self.assertTrue(any(
            item["image_id"] == "external_unknown"
            and "禁用或负面高风险" in item["reason"]
            for item in result["rejected"]
        ))

    def test_required_dj_content_does_not_promote_crowd_only_image(self):
        result = search_images_for_slot(
            {
                "slot_index": 1,
                "画面描述": "夜场DJ台正反打，展示打碟动作和前排观众",
                "镜头功能": "DJ演绎高光",
                "required_brief_content": ["DJ打碟与观众互动"],
            },
            {
                "项目名称": "城市电子音乐节",
                "脚本类型": "音乐节",
                "行业或活动类别": "电子音乐节",
            },
            [{
                "image_id": "dj",
                "原图路径": "/tmp/dj.png",
                "asset_role": "source_reference",
            }, {
                "image_id": "crowd",
                "原图路径": "/tmp/crowd.png",
                "asset_role": "source_reference",
            }, {
                "image_id": "dj_stage",
                "原图路径": "/tmp/dj-stage.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "dj",
                "caption": "DJ在混音台打碟并挥手带动前排观众互动",
                "tags": ["DJ", "打碟", "混音台", "观众"],
                "brand_names": ["无"],
                "source": "fixture",
            }, {
                "image_id": "crowd",
                "caption": "音乐节观众在人群中挥手欢呼互动",
                "tags": ["观众", "人群", "互动"],
                "brand_names": ["无"],
                "source": "fixture",
            }, {
                "image_id": "dj_stage",
                "caption": "电子音乐节舞台中央设置DJ台，台下观众挥手互动",
                "tags": ["DJ台", "舞台", "观众"],
                "brand_names": ["无"],
                "source": "fixture",
            }],
        )

        self.assertEqual(result["candidates"][0]["image_id"], "dj")
        self.assertEqual(
            result["candidates"][0]["required_content_hits"],
            ["DJ打碟与观众互动"],
        )
        crowd = next(item for item in result["candidates"] if item["image_id"] == "crowd")
        self.assertEqual(crowd["required_content_hits"], [])
        dj_stage = next(item for item in result["candidates"] if item["image_id"] == "dj_stage")
        self.assertEqual(dj_stage["required_content_hits"], [])

    def test_required_guitar_content_does_not_promote_other_instrument(self):
        result = search_images_for_slot(
            {
                "slot_index": 1,
                "画面描述": "草原歌手抱吉他演唱",
                "镜头功能": "歌手演出建立",
                "required_brief_content": ["草原歌手吉他演唱"],
            },
            {
                "项目名称": "草原民谣音乐节",
                "脚本类型": "音乐节",
            },
            [{
                "image_id": "guitar",
                "原图路径": "/tmp/guitar.png",
                "asset_role": "source_reference",
            }, {
                "image_id": "flute",
                "原图路径": "/tmp/flute.png",
                "asset_role": "source_reference",
            }],
            [{
                "image_id": "guitar",
                "caption": "男歌手在草原和雪山前抱着吉他对麦克风演唱",
                "tags": ["草原", "歌手", "吉他", "演唱"],
                "brand_names": ["无"],
                "source": "fixture",
            }, {
                "image_id": "flute",
                "caption": "民族服饰表演者在草原吹奏传统管乐器",
                "tags": ["草原", "表演者", "管乐器", "演奏"],
                "brand_names": ["无"],
                "source": "fixture",
            }],
        )

        self.assertEqual(result["candidates"][0]["image_id"], "guitar")
        flute = next(item for item in result["candidates"] if item["image_id"] == "flute")
        self.assertEqual(flute["required_content_hits"], [])

    def test_required_compounds_promote_visual_dinner_and_runway_evidence(self):
        scenarios = (
            (
                "晚宴长桌与花艺",
                "横移晚宴长桌、玻璃器皿、花艺与烛光",
                "宴会厅内长桌、酒杯、白色花艺和烛光布置",
                ["晚宴", "长桌", "花艺", "宴会厅"],
            ),
            (
                "明星走秀与服装造型",
                "暖金色T台中模特走秀，展示服装造型",
                "两位模特在T台走秀，展示长裙与印花服装造型",
                ["时装秀", "T台走秀", "模特", "服装"],
            ),
        )
        for required, description, caption, tags in scenarios:
            with self.subTest(required=required):
                result = search_images_for_slot(
                    {
                        "slot_index": 1,
                        "画面描述": description,
                        "镜头功能": "高光建立",
                        "required_brief_content": [required],
                    },
                    {
                        "项目名称": "跨类别测试",
                        "脚本类型": "年会" if "晚宴" in required else "明星活动",
                    },
                    [{
                        "image_id": "matching",
                        "原图路径": "/tmp/matching.png",
                        "asset_role": "source_reference",
                    }],
                    [{
                        "image_id": "matching",
                        "caption": caption,
                        "tags": tags,
                        "brand_names": ["无"],
                        "source": "fixture",
                    }],
                )

                self.assertEqual(
                    result["candidates"][0]["required_content_hits"],
                    [required],
                )
                self.assertEqual(
                    result["candidates"][0]["rank_components"]["required_content"],
                    40,
                )

    def test_package_category_must_match_brief_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source(source / "M+展会类通用脚本库_AI版v1.xlsx")
            package = create_asset_package(
                root / "package",
                package_id="exhibition-core",
                source_root=source,
                category_id="exhibition",
            )

            self.assertTrue(search_asset_package(package, "测试镜头", kind="script"))
            with self.assertRaisesRegex(AssetPackageError, "与当前运行类别"):
                package_media_for_brief(package, {"脚本类型": "发布会"})

    def test_child_resume_recovers_the_feedback_constraints_that_created_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            parent_dir = cache_dir / "runs" / "parent-run"
            child_dir = cache_dir / "runs" / "child-run"
            parent_dir.mkdir(parents=True)
            child_dir.mkdir(parents=True)
            feedback = store_user_feedback(
                parent_dir,
                run_id="parent-run",
                category="brand",
                comment="补齐当前品牌 Logo",
            )
            record_feedback_status(
                feedback["constraints_path"],
                "waiting_user",
                child_run_id="child-run",
            )
            (child_dir / "run_manifest.json").write_text(
                json.dumps({"run_id": "child-run", "parent_run_id": "parent-run"}),
                encoding="utf-8",
            )

            recovered = find_child_revision_constraints(cache_dir, "child-run")

            self.assertEqual(recovered, Path(feedback["constraints_path"]).resolve())

    def test_package_indexes_source_rows_and_keeps_media_id_after_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            media = root / "media"
            source.mkdir()
            media.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            first_image = media / "first.png"
            Image.new("RGB", (40, 30), "red").save(first_image)

            manifest = create_asset_package(
                root / "package",
                package_id="launch-core",
                source_root=source,
                media_roots=[media],
            )
            package = load_asset_package(manifest)
            first = search_asset_package(manifest, "测试镜头", kind="script")
            media_rows = search_asset_package(manifest, "", kind="media")

            self.assertTrue(first)
            self.assertEqual(first[0]["source_sheet"], "模版")
            self.assertEqual(package["index_counts"]["media_assets"], 1)
            media_id = media_rows[0]["media_id"]

            renamed = media / "renamed.png"
            first_image.rename(renamed)
            update_asset_package_index(manifest)
            updated = search_asset_package(manifest, "", kind="media")

            self.assertEqual(len(updated), 1)
            self.assertEqual(updated[0]["media_id"], media_id)
            self.assertEqual(Path(updated[0]["current_path"]).name, "renamed.png")

    def test_feedback_is_run_scoped_and_routes_to_minimum_content_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "parent"
            run_dir.mkdir(parents=True)
            result = store_user_feedback(
                run_dir,
                run_id="parent",
                category="style",
                scope="shot",
                shot_no="8",
                comment="更生活方式一些",
                desired_change="保留来源，重写画面表达",
                approve_text_only=True,
            )
            constraints = load_revision_constraints(result["constraints_path"])

            self.assertEqual(result["restart_stage"], "script_draft")
            self.assertEqual(constraints["parent_run_id"], "parent")
            self.assertEqual(constraints["user_feedback"][0]["shot_no"], "8")
            self.assertTrue(constraints["user_feedback"][0]["approve_text_only"])
            self.assertTrue(Path(result["feedback_path"]).is_file())
            self.assertEqual(
                load_feedback_status(result["constraints_path"])["status"],
                "received",
            )
            record_feedback_status(
                result["constraints_path"],
                "processing",
                child_job_id="job-1",
            )
            record_feedback_status(
                result["constraints_path"],
                "processing",
                child_job_id="job-1",
            )
            status = load_feedback_status(result["constraints_path"])
            status_events = Path(result["feedback_path"]).with_name(
                f"{result['feedback_id']}.events.jsonl"
            )
            self.assertEqual(status["status"], "processing")
            self.assertEqual(status["child_job_id"], "job-1")
            self.assertEqual(len(status_events.read_text(encoding="utf-8").splitlines()), 2)
            ledger = run_dir.parent.parent / "evolution_ledger.jsonl"
            event = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(event["event"], "feedback_received")
            self.assertEqual(event["details"]["reason"], "更生活方式一些")
            summary = summarize_evolution_ledgers([ledger])
            self.assertEqual(summary["event_counts"]["feedback_received"], 1)
            self.assertEqual(summary["feedback_categories"], {"style": 1})
            self.assertEqual(summary["feedback_statuses"], {"processing": 1})
            self.assertFalse(summary["automatic_rule_update"])

    def test_image_feedback_becomes_slot_scoped_visual_preference(self):
        result = _visual_preferences_from_feedback({
            "user_feedback": [{
                "category": "image",
                "shot_no": "8",
                "comment": "当前为空图",
                "desired_change": "优先生活方式社交画面",
            }]
        })

        self.assertEqual(result, {"8": "当前为空图；优先生活方式社交画面"})

    def test_feedback_categories_restart_only_their_minimum_semantic_stage(self):
        constraints = {"user_feedback": [
            {"category": "image", "shot_no": "8"},
            {"category": "style", "comment": "更克制"},
            {"category": "brand", "comment": "更换品牌"},
        ]}

        planning = _constraints_for_planning(constraints)
        drafting = _constraints_for_drafting(constraints)

        self.assertEqual([item["category"] for item in planning["user_feedback"]], ["brand"])
        self.assertEqual([item["category"] for item in drafting["user_feedback"]], ["style", "brand"])

    def test_image_only_feedback_does_not_invalidate_planning_or_drafting(self):
        constraints = {
            "schema_version": "mplus-revision-constraints/v1",
            "feedback_id": "feedback-1",
            "parent_run_id": "parent-1",
            "restart_stage": "visual_binding",
            "user_feedback": [{"category": "image", "shot_no": "8"}],
        }

        self.assertEqual(_constraints_for_planning(constraints), {})
        self.assertEqual(_constraints_for_drafting(constraints), {})

    def test_visual_audit_exclusions_do_not_invalidate_text_stages(self):
        constraints = {
            "exclude_visuals_by_slot": {"9": ["media_wrong"]},
        }

        self.assertEqual(_constraints_for_planning(constraints), {})
        self.assertEqual(_constraints_for_drafting(constraints), {})

    def test_image_only_feedback_reuses_frozen_plan_and_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "script_plan.json").write_text("{}", encoding="utf-8")
            (root / "script_draft.json").write_text("{}", encoding="utf-8")
            manifest_path = root / "run_manifest.json"
            manifest_path.write_text(json.dumps({"stages": {
                "script_plan": {"status": "succeeded"},
                "script_draft": {"status": "succeeded"},
            }}), encoding="utf-8")
            runtime = type("Runtime", (), {"run_dir": root, "manifest_path": manifest_path})()
            constraints = {
                "exclude_by_slot": {},
                "exclude_visuals_by_slot": {},
                "force_keep_slots": [],
                "global_excludes": [],
                "user_feedback": [{"category": "image", "shot_no": "8"}],
            }

            self.assertTrue(_can_reuse_upstream_for_image_feedback(runtime, constraints))
            constraints["user_feedback"].append({"category": "style"})
            self.assertFalse(_can_reuse_upstream_for_image_feedback(runtime, constraints))

    def test_visual_binding_child_reuses_successful_text_stage_without_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "script_plan.json").write_text("{}", encoding="utf-8")
            (root / "script_draft.json").write_text("{}", encoding="utf-8")
            manifest_path = root / "run_manifest.json"
            manifest_path.write_text(json.dumps({"stages": {
                "script_plan": {"status": "succeeded", "reused_from_run": "parent"},
                "script_draft": {"status": "succeeded", "reused_from_run": "parent"},
            }}), encoding="utf-8")
            runtime = type("Runtime", (), {
                "run_dir": root,
                "manifest_path": manifest_path,
            })()

            self.assertTrue(_can_reuse_stage_output(
                runtime,
                "script_plan",
                "script_plan.json",
                {},
            ))
            self.assertTrue(_can_reuse_stage_output(
                runtime,
                "script_draft",
                "script_draft.json",
                {},
            ))
            self.assertFalse(_can_reuse_stage_output(
                runtime,
                "script_draft",
                "script_draft.json",
                {"user_feedback": [{"category": "style"}]},
            ))

    def test_image_only_feedback_preserves_every_unrequested_parent_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            parent_dir = runs / "parent-run"
            child_dir = runs / "child-run"
            parent_dir.mkdir(parents=True)
            child_dir.mkdir()
            payload = {
                "script_plan_ref": str(parent_dir / "script_plan.json"),
                "briefs": [{"项目名称": "测试"}],
                "brief": {"项目名称": "测试"},
                "constraints": {},
                "source_view": "human",
                "script_rows": [
                    {"镜号": "1", "画面示意": "parent-1.png"},
                    {"镜号": "2", "画面示意": ""},
                    {"镜号": "3", "画面示意": "parent-3.png"},
                ],
                "detail_rows": [
                    {"slot_index": 1, "图片绑定说明": "parent-1"},
                    {"slot_index": 2, "图片绑定说明": "parent-2"},
                    {"slot_index": 3, "图片绑定说明": "parent-3"},
                ],
                "decisions": [],
                "template_slots": [],
                "source_refs": [],
                "visual_asset_rows": [
                    {"slot_index": 1, "image_id": "parent-1", "原图路径": "parent-1.png"},
                    {"slot_index": 2, "image_id": "", "原图路径": ""},
                    {"slot_index": 3, "image_id": "parent-3", "原图路径": "parent-3.png"},
                ],
                "bindings": [],
            }
            (parent_dir / "visual_binding.json").write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            runtime = type("Runtime", (), {"run_dir": child_dir})()
            scripts = [
                {"镜号": "1", "画面示意": "changed-1.png"},
                {"镜号": "2", "画面示意": "selected-2.png"},
                {"镜号": "3", "画面示意": ""},
            ]
            details = [
                {"slot_index": 1, "图片绑定说明": "changed-1"},
                {"slot_index": 2, "图片绑定说明": "selected-2"},
                {"slot_index": 3, "图片绑定说明": "cleared-3"},
            ]
            assets = [
                {"slot_index": 1, "image_id": "changed-1", "原图路径": "changed-1.png"},
                {"slot_index": 2, "image_id": "selected-2", "原图路径": "selected-2.png"},
                {"slot_index": 3, "image_id": "", "原图路径": ""},
            ]

            scripts, details, assets = _preserve_unaffected_parent_visuals(
                runtime,
                {
                    "parent_run_id": "parent-run",
                    "user_feedback": [{"category": "image", "shot_no": "2"}],
                },
                scripts,
                details,
                assets,
            )

            self.assertEqual([row["画面示意"] for row in scripts], [
                "parent-1.png", "selected-2.png", "parent-3.png",
            ])
            self.assertEqual([row["image_id"] for row in assets], [
                "parent-1", "selected-2", "parent-3",
            ])

    def test_child_feedback_keeps_prior_revision_constraints(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "parent"
            run_dir.mkdir(parents=True)
            first = store_user_feedback(
                run_dir,
                run_id="parent",
                category="image",
                scope="shot",
                shot_no="8",
                comment="补充第8镜图片",
            )
            second = store_user_feedback(
                run_dir,
                run_id="child",
                category="image",
                scope="shot",
                shot_no="9",
                comment="补充第9镜图片",
                prior_constraints_path=first["constraints_path"],
            )

            constraints = load_revision_constraints(second["constraints_path"])

        self.assertEqual(
            [item["shot_no"] for item in constraints["user_feedback"]],
            ["8", "9"],
        )

    def test_targeted_detail_feedback_preserves_unrelated_text_and_all_visuals(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            parent_dir = runs / "parent-run"
            child_dir = runs / "child-run"
            parent_dir.mkdir(parents=True)
            child_dir.mkdir(parents=True)
            payload = {
                "script_plan_ref": str(parent_dir / "script_plan.json"),
                "briefs": [], "brief": {}, "constraints": {}, "source_view": "ai-spec",
                "quality_profile": "client-review", "text_model": "fixture",
                "overall_summary": "", "pending_confirmations": [], "text_usage": {},
                "script_rows": [
                    {"镜号": "1", "画面描述": "父正文1", "画面示意": "parent-1.png"},
                    {"镜号": "2", "画面描述": "父正文2", "画面示意": "parent-2.png"},
                ],
                "detail_rows": [
                    {"slot_index": 1, "采用来源证据": "父来源1", "图片绑定说明": "parent-1"},
                    {"slot_index": 2, "采用来源证据": "父来源2", "图片绑定说明": "parent-2"},
                ],
                "decisions": [], "template_slots": [], "source_refs": [],
                "visual_asset_rows": [
                    {"slot_index": 1, "image_id": "parent-1", "原图路径": "parent-1.png"},
                    {"slot_index": 2, "image_id": "parent-2", "原图路径": "parent-2.png"},
                ],
                "bindings": [],
            }
            (parent_dir / "visual_binding.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            runtime = type("Runtime", (), {"run_dir": child_dir})()
            constraints = {
                "parent_run_id": "parent-run",
                "user_feedback": [{
                    "category": "detail", "scope": "shot", "shot_no": "2",
                }],
            }
            scripts = [
                {"镜号": "1", "画面描述": "新正文1", "画面示意": "changed-1.png"},
                {"镜号": "2", "画面描述": "新正文2", "画面示意": "changed-2.png"},
            ]
            details = [
                {"slot_index": 1, "采用来源证据": "新来源1", "图片绑定说明": "changed-1"},
                {"slot_index": 2, "采用来源证据": "新来源2", "图片绑定说明": "changed-2"},
            ]
            assets = [
                {"slot_index": 1, "image_id": "changed-1", "原图路径": "changed-1.png"},
                {"slot_index": 2, "image_id": "changed-2", "原图路径": "changed-2.png"},
            ]

            self.assertTrue(_has_targeted_non_visual_feedback(constraints))
            scripts, details, assets = _preserve_unaffected_parent_visuals(
                runtime, constraints, scripts, details, assets, visual_only=True,
            )

            self.assertEqual(scripts[0]["画面描述"], "父正文1")
            self.assertEqual(scripts[0]["画面示意"], "parent-1.png")
            self.assertEqual(details[0]["采用来源证据"], "新来源1")
            self.assertEqual(details[0]["图片绑定说明"], "parent-1")
            self.assertEqual(scripts[1]["画面描述"], "新正文2")
            self.assertEqual(scripts[1]["画面示意"], "parent-2.png")
            self.assertEqual([row["image_id"] for row in assets], ["parent-1", "parent-2"])

            mixed_scripts, mixed_details, mixed_assets = _preserve_unaffected_parent_visuals(
                runtime,
                {
                    "parent_run_id": "parent-run",
                    "user_feedback": [
                        {"category": "image", "scope": "shot", "shot_no": "1"},
                        {"category": "detail", "scope": "shot", "shot_no": "2"},
                    ],
                },
                [
                    {"镜号": "1", "画面描述": "模型改坏镜1", "画面示意": "selected-1.png"},
                    {"镜号": "2", "画面描述": "只改镜2", "画面示意": "changed-2.png"},
                ],
                [
                    {"slot_index": 1, "图片绑定说明": "selected-1"},
                    {"slot_index": 2, "图片绑定说明": "changed-2"},
                ],
                [
                    {"slot_index": 1, "image_id": "selected-1", "原图路径": "selected-1.png"},
                    {"slot_index": 2, "image_id": "changed-2", "原图路径": "changed-2.png"},
                ],
                visual_only=True,
            )

            self.assertEqual(mixed_scripts[0]["画面描述"], "父正文1")
            self.assertEqual(mixed_scripts[0]["画面示意"], "selected-1.png")
            self.assertEqual(mixed_scripts[1]["画面描述"], "只改镜2")
            self.assertEqual(mixed_scripts[1]["画面示意"], "parent-2.png")
            self.assertEqual(
                [row["image_id"] for row in mixed_assets],
                ["selected-1", "parent-2"],
            )

    def test_package_media_can_be_selected_by_brief_without_fixed_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            media = root / "media" / "测试品牌"
            metadata = root / "metadata"
            source.mkdir()
            media.mkdir(parents=True)
            metadata.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            image_path = media / "brand.png"
            Image.new("RGB", (1920, 1080), "white").save(image_path)
            (metadata / "image_manifest.jsonl").write_text(json.dumps({
                "image_id": "legacy_id", "原图路径": str(image_path)
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            (metadata / "image_analysis.minimax.jsonl").write_text(json.dumps({
                "image_id": "legacy_id", "caption": "测试品牌 logo", "tags": ["logo"],
                "brand_names": ["测试品牌"], "review_status": "reviewed",
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            package = create_asset_package(
                root / "package",
                package_id="launch-core",
                source_root=source,
                media_roots=[root / "media"],
                metadata_roots=[metadata],
            )

            manifest, analysis = package_media_for_brief(
                package,
                {"项目名称": "测试品牌发布会", "品牌或客户": "测试品牌"},
            )
            summary = asset_package_index_summary(package)

            connection = sqlite3.connect(load_asset_package(package)["index_path"])
            try:
                connection.execute(
                    "UPDATE media_assets SET analysis_json=?",
                    (json.dumps({
                        "caption": "供应商拒绝",
                        "review_status": "blocked",
                        "vision_recognition_status": "failed",
                    }, ensure_ascii=False),),
                )
                connection.commit()
            finally:
                connection.close()
            failed_summary = asset_package_index_summary(package)

        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["image_id"], analysis[0]["image_id"])
        self.assertTrue(manifest[0]["image_id"].startswith("media_"))
        self.assertEqual(summary["media_assets"], 1)
        self.assertEqual(summary["media_with_analysis"], 1)
        self.assertEqual(summary["media_with_successful_analysis"], 1)
        self.assertEqual(summary["media_with_failed_analysis"], 0)
        self.assertEqual(summary["media_pending_analysis"], 0)
        self.assertEqual(failed_summary["media_with_analysis"], 1)
        self.assertEqual(failed_summary["media_with_successful_analysis"], 0)
        self.assertEqual(failed_summary["media_with_failed_analysis"], 1)
        self.assertEqual(failed_summary["media_pending_analysis"], 0)

    def test_unchanged_package_update_does_not_reopen_excel_images_or_analysis_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            media = root / "media" / "测试品牌"
            metadata = root / "metadata"
            source.mkdir()
            media.mkdir(parents=True)
            metadata.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            image_path = media / "brand.png"
            Image.new("RGB", (1920, 1080), "white").save(image_path)
            (metadata / "image_manifest.jsonl").write_text(json.dumps({
                "image_id": "legacy_id", "原图路径": str(image_path)
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            (metadata / "image_analysis.minimax.jsonl").write_text(json.dumps({
                "image_id": "legacy_id", "caption": "测试品牌 logo", "tags": ["logo"],
                "brand_names": ["测试品牌"], "review_status": "reviewed",
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            package = create_asset_package(
                root / "package", package_id="launch-core", source_root=source,
                media_roots=[root / "media"], metadata_roots=[metadata],
            )

            with patch("mvp_v2.asset_package._read_jsonl") as read_jsonl:
                counts = update_asset_package_index(package)

        read_jsonl.assert_not_called()
        self.assertEqual(counts["media_hashed"], 0)
        self.assertEqual(counts["metadata_files_read"], 0)
        self.assertEqual(counts["metadata_files_reused"], 1)
        self.assertTrue(counts["source_workbook_hash_reused"])

    def test_script_catalog_reads_index_without_reopening_xlsx(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            package = create_asset_package(
                root / "package",
                package_id="launch-core",
                source_root=source,
            )

            with patch(
                "mvp_v2.asset_package.load_template_slots",
                side_effect=AssertionError("不应重新读取 xlsx"),
            ):
                template, creative, source_record = package_script_catalog(
                    package,
                    expected_category_id="launch",
                    source_view="human",
                )

        self.assertEqual(len(template), 1)
        self.assertEqual(len(creative), 1)
        self.assertEqual(source_record["source_view"], "human")

    def test_changed_source_requires_package_update_before_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            workbook = source / "M+发布会类通用脚本库.xlsx"
            _write_source(workbook)
            package = create_asset_package(
                root / "package",
                package_id="launch-core",
                source_root=source,
            )
            workbook.touch()

            with self.assertRaisesRegex(AssetPackageError, "工作簿已变化"):
                asset_package_source_record(
                    package,
                    expected_category_id="launch",
                )

    def test_source_fingerprint_ignores_noop_index_timestamp_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            package = create_asset_package(
                root / "package",
                package_id="launch-core",
                source_root=source,
            )
            before = asset_package_source_record(
                package,
                expected_category_id="launch",
            )["index_fingerprint"]

            manifest_path = Path(package)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["updated_at"] = "2099-01-01T00:00:00+00:00"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            after = asset_package_source_record(
                package,
                expected_category_id="launch",
            )["index_fingerprint"]

        self.assertEqual(after, before)

    def test_group_photo_wording_search_preserves_entities_sources_and_index(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / "source"; source.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            image_path = root / "reference.png"
            Image.new("RGB", (40, 30), "blue").save(image_path)
            shots = [SourceShot(script_type="发布会", source_file="M+发布会类通用脚本库.xlsx",
                source_sheet="模版", source_row=7+i, shot_no=str(i+1), structure="收束",
                visual_desc=text, shot_function=text, reference_image_path=str(image_path))
                for i, text in enumerate(("嘉宾合影", "嘉宾合照"))]
            with patch("mvp_v2.asset_package.load_ai_spec_shots", return_value=(shots, [])):
                manifest = create_asset_package(root / "package", package_id="launch-core", source_root=source)
            package = load_asset_package(manifest); index = Path(package["index_path"])
            before = hashlib.sha256(index.read_bytes()).hexdigest()
            for kind, key in (("script", "asset_id"), ("media", "media_id")):
                original = search_asset_package(manifest, "合影", kind=kind, limit=20)
                alternate = search_asset_package(manifest, "合照", kind=kind, limit=20)
                self.assertTrue(original)
                self.assertEqual({r[key] for r in original}, {r[key] for r in alternate})
                self.assertEqual(len(original), len({r[key] for r in original}))
                self.assertEqual(len(search_asset_package(manifest, "合照", kind=kind, limit=1)), 1)
                self.assertEqual(search_asset_package(manifest, "签字", kind=kind), [])
            self.assertEqual(len(search_asset_package(manifest, "合照", kind="script")), 2)
            media = search_asset_package(manifest, "合照", kind="media")[0]
            self.assertEqual({r["source_row"] for r in media["source_links"]}, {7, 8})
            self.assertEqual(hashlib.sha256(index.read_bytes()).hexdigest(), before)

    def test_source_image_is_searchable_by_row_semantics_with_triplet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            image_path = root / "灯海.png"
            Image.new("RGB", (40, 30), "blue").save(image_path)
            shot = SourceShot(
                script_type="发布会", source_file="M+发布会类通用脚本库.xlsx",
                source_sheet="模版", source_row=7, shot_no="1", structure="高潮",
                visual_desc="员工举起灯牌形成蓝色灯海",
                shot_function="员工灯海应援", visual_subject="员工、灯牌、舞台",
                tags="年会 应援", reference_image_path=str(image_path),
            )
            with patch(
                "mvp_v2.asset_package.load_ai_spec_shots",
                return_value=([shot], []),
            ):
                package = create_asset_package(
                    root / "package",
                    package_id="launch-core",
                    source_root=source,
                )

            rows = search_asset_package(package, "灯海应援", kind="media")
            counts = load_asset_package(package)["index_counts"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["semantic_basis"], "source_row")
        self.assertEqual(rows[0]["source_links"][0]["source_file"], "M+发布会类通用脚本库.xlsx")
        self.assertEqual(rows[0]["source_links"][0]["source_sheet"], "模版")
        self.assertEqual(rows[0]["source_links"][0]["source_row"], 7)
        self.assertEqual(counts["media_with_source_context"], 1)
        self.assertEqual(counts["media_source_links"], 1)

    def test_later_source_image_is_linked_without_reopening_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            image_path = root / "后补灯海.png"
            shot = SourceShot(
                script_type="发布会", source_file="M+发布会类通用脚本库.xlsx",
                source_sheet="模版", source_row=7, shot_no="1", structure="高潮",
                visual_desc="员工举起灯牌形成蓝色灯海",
                shot_function="员工灯海应援", reference_image_path=str(image_path),
            )
            with patch(
                "mvp_v2.asset_package.load_ai_spec_shots",
                return_value=([shot], []),
            ):
                package = create_asset_package(
                    root / "package",
                    package_id="launch-core",
                    source_root=source,
                )
            Image.new("RGB", (40, 30), "blue").save(image_path)

            counts = update_asset_package_index(package)
            rows = search_asset_package(package, "灯海应援", kind="media")

        self.assertFalse(counts["source_reindexed"])
        self.assertEqual(counts["media_hashed"], 1)
        self.assertEqual(counts["media_with_source_context"], 1)
        self.assertEqual(rows[0]["semantic_basis"], "source_row")
        self.assertEqual(rows[0]["source_links"][0]["source_row"], 7)

    def test_adding_one_image_hashes_only_the_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            media = root / "media"
            source.mkdir()
            media.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            Image.new("RGB", (40, 30), "red").save(media / "existing.png")
            package = create_asset_package(
                root / "package",
                package_id="launch-core",
                source_root=source,
                media_roots=[media],
            )
            Image.new("RGB", (40, 30), "blue").save(media / "new.png")

            counts = update_asset_package_index(package)

        self.assertEqual(counts["media_assets"], 2)
        self.assertEqual(counts["media_hashed"], 1)
        self.assertEqual(counts["media_reused"], 1)
        self.assertTrue(counts["source_workbook_hash_reused"])

    def test_indexed_image_analysis_skips_visual_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            manifest_path = cache / "image_manifest.jsonl"
            manifest_path.write_text(
                json.dumps({
                    "image_id": "media_existing",
                    "原图路径": str(root / "existing.png"),
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            media_manifest = AutoPipelineMediaManifest(
                cache_dir=cache,
                manifest_path=manifest_path,
                report_path=cache / "report.json",
                manifest_rows=[{
                    "image_id": "media_existing",
                    "原图路径": str(root / "existing.png"),
                }],
            )
            indexed_analysis = [{
                "image_id": "media_existing",
                "caption": "已完成图片理解",
                "tags": ["舞台"],
                "brand_names": ["测试品牌"],
                "source": "minimax",
            }]

            with patch(
                "mvp_v2.auto_pipeline.write_minimax_analysis_incremental"
            ) as provider_call:
                result = analyze_auto_pipeline_media_manifest(
                    media_manifest,
                    api_key="",
                    seed_analysis_rows=indexed_analysis,
                )

        provider_call.assert_not_called()
        self.assertEqual(result.reused_analysis_count, 1)
        self.assertEqual(result.generated_analysis_count, 0)

    def test_visual_provider_can_be_injected_without_changing_pipeline_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            image_path = root / "new.png"
            Image.new("RGB", (40, 30), "white").save(image_path)
            manifest_rows = [{"image_id": "new-image", "原图路径": str(image_path)}]
            manifest_path = cache / "image_manifest.jsonl"
            manifest_path.write_text(
                json.dumps(manifest_rows[0], ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            media_manifest = AutoPipelineMediaManifest(
                cache_dir=cache,
                manifest_path=manifest_path,
                report_path=cache / "report.json",
                manifest_rows=manifest_rows,
            )
            calls = []

            def fake_provider(rows, output, **kwargs):
                calls.append({"rows": len(rows), "model": kwargs.get("model")})
                Path(output).write_text(json.dumps({
                    "image_id": "new-image",
                    "caption": "新图片已分析",
                    "tags": ["发布会"],
                    "brand_names": ["无"],
                    "source": "minimax",
                }, ensure_ascii=False) + "\n", encoding="utf-8")
                return 1

            result = analyze_auto_pipeline_media_manifest(
                media_manifest,
                api_key="",
                model="MiniMax-M3",
                vision_provider=fake_provider,
            )

        self.assertEqual(calls, [{"rows": 1, "model": "MiniMax-M3"}])
        self.assertEqual(result.generated_analysis_count, 1)

    def test_image_feedback_can_fill_a_blank_visual_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "brand-logo.png"
            Image.new("RGB", (1920, 1080), "white").save(image_path)
            script_rows = [{
                "镜号": "1",
                "画面示意": "",
                "画面描述": "测试品牌标识进入画面",
            }]
            detail_rows = [{"slot_index": 1, "模板镜号": "1", "替换方式": "KEEP"}]
            manifest = [{
                "image_id": "img_logo",
                "原图路径": str(image_path),
                "预览路径": str(image_path),
                "来源文件": "测试素材",
                "来源 sheet": "图片库",
                "来源行": 1,
                "是否低清": False,
            }]
            analysis = [{
                "image_id": "img_logo",
                "caption": "测试品牌 logo 标识特写",
                "tags": ["测试品牌", "logo", "品牌标识"],
                "brand_names": ["测试品牌"],
                "ocr_text": "测试品牌",
                "negative_flags": [],
                "source": "fixture",
            }]

            rows, _, assets = rematch_visual_assets(
                script_rows,
                detail_rows,
                [],
                {},
                [image_path],
                manifest_rows=manifest,
                analysis_rows=analysis,
                brief={"项目名称": "测试发布会", "品牌或客户": "测试品牌"},
                visual_preferences_by_slot={"1": "必须补充品牌 logo 画面"},
            )

        self.assertEqual(rows[0]["画面示意"], str(image_path))
        self.assertEqual(assets[0]["image_id"], "img_logo")

    def test_candidate_search_and_manual_choice_use_the_same_safety_filtered_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            image_path = run_dir / "brand-logo.png"
            Image.new("RGB", (1920, 1080), "white").save(image_path)
            (run_dir / "script_draft.json").write_text(json.dumps({
                "brief": {
                    "项目名称": "测试发布会",
                    "品牌或客户": "测试品牌",
                    "脚本类型": "发布会",
                },
                "script_rows": [{"镜号": "1", "画面描述": "测试品牌 logo 标识进入画面"}],
                "detail_rows": [{"slot_index": 1, "模板镜号": "1"}],
            }, ensure_ascii=False), encoding="utf-8")
            (run_dir / "image_manifest.jsonl").write_text(json.dumps({
                "image_id": "img_logo", "原图路径": str(image_path), "预览路径": str(image_path),
                "来源文件": "测试素材", "来源 sheet": "图片库", "来源行": 1, "是否低清": False,
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            (run_dir / "image_analysis.minimax.jsonl").write_text(json.dumps({
                "image_id": "img_logo", "caption": "测试品牌 logo 品牌标识特写",
                "tags": ["测试品牌", "logo", "品牌标识"], "brand_names": ["测试品牌"],
                "ocr_text": "测试品牌", "negative_flags": [], "source": "fixture",
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            (run_dir / "visual_binding.json").write_text(json.dumps({
                "bindings": [{"slot_index": 1, "image_id": "", "original_path": ""}],
                "visual_asset_rows": [{"slot_index": 1, "image_id": "", "原图路径": ""}],
            }, ensure_ascii=False), encoding="utf-8")

            result = candidates_for_run(run_dir, shot_no="1")
            rows, _, assets = rematch_visual_assets(
                [{"镜号": "1", "画面示意": "", "画面描述": "测试品牌 logo 标识进入画面"}],
                [{"slot_index": 1, "模板镜号": "1", "替换方式": "KEEP"}],
                [], {}, [image_path],
                manifest_rows=[{
                    "image_id": "img_logo", "原图路径": str(image_path), "预览路径": str(image_path),
                    "来源文件": "测试素材", "来源 sheet": "图片库", "来源行": 1, "是否低清": False,
                }],
                analysis_rows=[{
                    "image_id": "img_logo", "caption": "测试品牌 logo 品牌标识特写",
                    "tags": ["测试品牌", "logo", "品牌标识"], "brand_names": ["测试品牌"],
                    "ocr_text": "测试品牌", "negative_flags": [], "source": "fixture",
                }],
                brief={"项目名称": "测试发布会", "品牌或客户": "测试品牌"},
                visual_preferences_by_slot={"1": "补图"},
                forced_visuals_by_slot={"1": {"image_id": "img_logo", "path": str(image_path)}},
            )

        self.assertEqual(result["candidates"][0]["image_id"], "img_logo")
        self.assertEqual(result["resolution_status"], "visual_resolution_required")
        self.assertEqual(result["shot_context"]["visual_description"], "测试品牌 logo 标识进入画面")
        self.assertEqual(result["script_type"], "发布会")
        self.assertEqual(result["pool"]["indexed"], 1)
        self.assertEqual(result["pool"]["visually_analyzed"], 1)
        self.assertEqual(result["pool"]["visual_succeeded"], 1)
        self.assertEqual(result["pool"]["visual_failed"], 0)
        self.assertEqual(result["pool"]["eligible"], 1)
        self.assertEqual(rows[0]["画面示意"], str(image_path))
        self.assertIn("人工从安全候选中选定", assets[0]["绑定说明"])

    def test_manual_safe_candidate_between_review_and_auto_threshold_is_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "social.png"
            Image.new("RGB", (1920, 1080), "white").save(image_path)
            script_rows = [{"镜号": "8", "结构": "人气\n与现场互动", "画面示意": "", "画面描述": "展陈区嘉宾近距离观看体验产品，人群交流互动补充现场氛围"}]
            detail_rows = [{
                "slot_index": 8, "模板镜号": "8", "槽位功能": "服务社交氛围",
                "必须视觉证据": "服务或茶歇场景、现场宾客", "替换方式": "KEEP",
                "允许素材": "茶歇、服务台、饮品互动、宾客交流",
                "禁止素材": "演讲正面、颁发仪式、片尾落版",
            }]
            manifest = [{
                "image_id": "img_social", "原图路径": str(image_path), "预览路径": str(image_path),
                "来源文件": "测试素材", "来源 sheet": "图片库", "来源行": 1, "是否低清": False,
            }]
            analysis = [{
                "image_id": "img_social",
                "caption": "霓虹灯装饰的室内场景中，人们围坐在摆放着火锅和餐具的木桌旁，气氛热闹。一位女士正用手机拍照，整体呈现出发布会或派对活动的现场感。",
                "tags": ["室内场景", "发布会", "霓虹灯", "火锅", "聚餐", "霓虹灯装饰", "木桌", "手机拍照", "派对氛围"],
                "brand_names": ["无"], "negative_flags": [], "source": "fixture",
            }]
            result = search_images_for_slot(
                {**detail_rows[0], **script_rows[0]}, {"项目名称": "测试发布会"}, manifest, analysis
            )
            rows, _, assets = rematch_visual_assets(
                script_rows, detail_rows, [], {}, [image_path],
                manifest_rows=manifest, analysis_rows=analysis,
                brief={"项目名称": "测试发布会"},
                visual_preferences_by_slot={"8": "选择安全候选"},
                forced_visuals_by_slot={"8": {"image_id": "img_social", "path": str(image_path)}},
            )

        self.assertGreaterEqual(result["candidates"][0]["score"], 55)
        self.assertLess(result["candidates"][0]["score"], 80)
        self.assertEqual(rows[0]["画面示意"], str(image_path))
        self.assertIn("人工从安全候选中选定", assets[0]["绑定说明"])

    def test_image_feedback_uses_displayed_shot_number_when_logical_slot_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "phone-recording.png"
            Image.new("RGB", (1920, 1080), "white").save(image_path)
            script_rows = [{
                "镜号": "12",
                "结构": "核心宣讲\n与仪式",
                "画面示意": "",
                "画面描述": "观众举起手机记录舞台高光时刻，手机屏幕捕捉发布内容",
            }]
            detail_rows = [{
                "slot_index": 12,
                "逻辑槽位": 13,
                "模板镜号": "13",
                "槽位功能": "观众反应/传播感",
                "必须视觉证据": "观众手机、舞台或发布内容被记录",
                "允许素材": "手机拍摄、观众录屏、舞台内容",
                "禁止素材": "签到、茶歇、片尾落版",
                "替换方式": "KEEP",
            }]
            manifest = [{
                "image_id": "img_phone", "原图路径": str(image_path),
                "预览路径": str(image_path), "来源文件": "测试素材",
                "来源 sheet": "图片库", "来源行": 1, "是否低清": False,
            }]
            analysis = [{
                "image_id": "img_phone",
                "caption": "观众横握手机录屏拍摄发布会舞台演讲者",
                "tags": ["发布会", "手机拍摄", "手机录屏", "观众", "舞台"],
                "brand_names": ["无"], "negative_flags": [], "source": "fixture",
            }]

            rows, _, assets = rematch_visual_assets(
                script_rows, detail_rows, [], {}, [image_path],
                manifest_rows=manifest, analysis_rows=analysis,
                brief={"项目名称": "测试发布会", "脚本类型": "发布会"},
                visual_preferences_by_slot={"12": "采用此安全候选"},
                forced_visuals_by_slot={
                    "12": {"image_id": "img_phone", "path": str(image_path)},
                },
            )

        self.assertEqual(rows[0]["画面示意"], str(image_path))
        self.assertEqual(assets[0]["image_id"], "img_phone")
        self.assertIn("人工从安全候选中选定", assets[0]["绑定说明"])

    def test_forced_visual_is_revalidated_against_full_safe_candidate_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = root / "selected.png"
            Image.new("RGB", (40, 30), "blue").save(selected)
            candidates = [
                {
                    "image_id": f"other-{index}",
                    "原图路径": str(root / f"other-{index}.png"),
                    "score": 90,
                }
                for index in range(8)
            ] + [{
                "image_id": "selected-id",
                "原图路径": str(selected),
                "score": 64,
                "summary": "主舞台灯光汇聚",
                "tags": ["发布会", "舞台"],
            }]
            with patch(
                "mvp_v2.image_retrieval.search_images_for_slot",
                return_value={"candidates": candidates, "gap_reason": ""},
            ) as search:
                result = _best_manifest_candidate(
                    {"镜号": "3", "画面描述": "主舞台转场"},
                    {"slot_index": 3},
                    {"项目名称": "测试", "脚本类型": "发布会"},
                    [{"image_id": f"asset-{index}"} for index in range(9)],
                    [],
                    set(),
                    forced_visual={
                        "image_id": "selected-id",
                        "path": str(selected),
                    },
                )

        self.assertEqual(search.call_args.kwargs["top_k"], 9)
        self.assertEqual(result["image_id"], "selected-id")

    def test_visual_binding_never_leaves_an_unclassified_blank(self):
        script = [{"镜号": "1", "画面描述": "已审核装置空镜"}]
        detail = [{
            "slot_index": 1,
            "采用来源文件": "脚本库.xlsx",
            "采用来源 sheet": "模版",
            "采用来源行": 10,
        }]

        unresolved = _bindings(script, detail, [])
        approved = _bindings(
            script,
            detail,
            [],
            constraints={"user_feedback": [{
                "category": "image",
                "shot_no": "1",
                "approve_text_only": True,
            }]},
        )

        self.assertEqual(unresolved[0]["resolution_status"], "visual_resolution_required")
        self.assertTrue(unresolved[0]["document_basis_available"])
        self.assertEqual(approved[0]["resolution_status"], "text_only_approved")


def _write_source(path: Path) -> None:
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    template.cell(5, 1, "结构")
    template.cell(5, 2, "镜号")
    for col, value in enumerate(["画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注"], 3):
        template.cell(6, col, value)
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
    for col, value in enumerate(["画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"], 4):
        creative.cell(6, col, value)
    creative.cell(7, 1, "C001")
    creative.cell(7, 2, "开场造势")
    creative.cell(7, 5, "转场")
    creative.cell(7, 6, "实拍")
    creative.cell(7, 7, "全景")
    creative.cell(7, 8, "推进")
    creative.cell(7, 9, "测试创意画面")
    workbook.save(path)


def _write_source_with_reference_image(path: Path, image_path: Path) -> None:
    Image.new("RGB", (1280, 720), "white").save(image_path)
    _write_source(path)
    workbook = load_workbook(path)
    image_bytes = io.BytesIO(image_path.read_bytes())
    workbook["模版"].add_image(SpreadsheetImage(image_bytes), "C7")
    workbook.save(path)
    workbook.close()
    image_bytes.close()


if __name__ == "__main__":
    unittest.main()
