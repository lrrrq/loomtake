import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from mvp_v2.audit import audit_generation
from mvp_v2.audit import _required_term_present
from mvp_v2.brand_policy import (
    adapt_source_text_to_project,
    brand_evidence_unverified,
    conflicting_brand_names,
    conflicting_brands,
    source_brand_leaks,
    unknown_brand_mentions,
)
from mvp_v2.excel_io import load_creative_variants
from mvp_v2.generator import generate_template_rows
from mvp_v2.image_retrieval import _brand_conflict_reason
from mvp_v2.models import SourceShot, TemplateDecision
from mvp_v2.scoring import match_template_slots


class GenericBrandPolicyTests(unittest.TestCase):
    def test_product_control_labels_are_not_invented_brands(self):
        for label in ("面板", "控制面板", "按键", "容量"):
            self.assertEqual(unknown_brand_mentions("匿名客户", f"镜头横移带过{label}标识"), [])
        for text in ("镜头横移带过另一科技标识", "镜头扫过另一科技品牌标识", "展示面板品牌标识"):
            self.assertTrue(unknown_brand_mentions("匿名客户", text), text)

    def test_generic_venue_possessive_does_not_invent_a_customer_brand(self):
        for place in ("活动现场", "发布会现场", "会场入口", "主会场"):
            with self.subTest(place=place):
                self.assertEqual(unknown_brand_mentions("匿名发布会", f"{place}的品牌标识入画"), [])
        self.assertEqual(unknown_brand_mentions("匿名发布会", "星河科技的品牌标识入画"), ["星河科技"])

    def test_wayfinding_sign_is_not_an_unknown_brand_but_explicit_brand_is(self):
        for text in (
            "拍摄建议：展示主会场入口处的指引标识。",
            "展示入口指引标识",
            "展示场馆出口处的指引标识",
            "展示指引标识",
        ):
            with self.subTest(text=text):
                self.assertEqual(unknown_brand_mentions("匿名发布会", text), [])
        for brand in ("星河科技", "指引科技", "指引"):
            with self.subTest(brand=brand):
                self.assertEqual(
                    unknown_brand_mentions("匿名发布会", f"展示{brand}品牌标识"),
                    [brand],
                )

    def test_generic_screen_has_brand_logo_phrase_is_not_a_brand_name(self):
        self.assertEqual(
            unknown_brand_mentions("林氏家居", "舞台屏幕上有品牌logo和汉字元素"),
            [],
        )

    def test_lighting_context_before_brand_logo_is_not_a_brand_name(self):
        self.assertEqual(
            unknown_brand_mentions("林氏家居", "霓虹光影中品牌logo入画"),
            [],
        )

    def test_camera_movement_before_brand_wall_is_not_a_brand_name(self):
        self.assertEqual(
            unknown_brand_mentions("匿名光学", "穿越机掠过品牌墙，画面切至主舞台"),
            [],
        )

    def test_project_logo_action_is_not_misread_as_an_unknown_brand(self):
        for text in (
            "舞台大屏呈现品牌LOGO",
            "主屏亮出当前项目LOGO",
            "清晰品牌标识定格收束",
        ):
            with self.subTest(text=text):
                self.assertEqual(unknown_brand_mentions("匿名纵向闭环项目", text), [])

    def test_screen_reveal_action_keeps_generic_brand_neutral_and_explicit_brand_detectable(self):
        for action in ("显现", "浮现"):
            with self.subTest(action=action):
                self.assertEqual(unknown_brand_mentions("匿名发布会", f"主视觉屏幕{action}品牌标识"), [])
                self.assertEqual(unknown_brand_mentions("匿名发布会", f"主视觉屏幕{action}星河科技品牌标识"), ["星河科技"])

    def test_explicit_unknown_brand_before_logo_remains_blocked(self):
        for text in ("主屏亮出星河科技品牌LOGO", "主屏亮出星河科技的品牌LOGO"):
            with self.subTest(text=text):
                self.assertEqual(
                    unknown_brand_mentions("匿名纵向闭环项目", text),
                    ["星河科技"],
                )

    def test_required_content_ignores_display_spacing_around_latin_terms(self):
        self.assertTrue(_required_term_present(
            "匿名光学 Logo",
            "冷蓝光划过，匿名光学Logo与主题依次浮现",
        ))
        self.assertTrue(_required_term_present(
            "结尾主视觉 KV",
            "画面闪白，结尾主视觉KV浮现并定格",
        ))

    def test_unknown_brief_brand_rejects_known_other_brand(self):
        self.assertEqual(conflicting_brands("星河科技", "LINSY HOME 品牌墙"), ["林氏家居"])

    def test_explicit_unknown_visual_brand_is_compared_to_brief_brand(self):
        self.assertEqual(
            conflicting_brand_names("星河科技", ["另一科技"]),
            ["另一科技"],
        )
        self.assertEqual(conflicting_brand_names("星河科技", ["星河科技"]), [])

    def test_cross_brand_source_is_adapted_to_unknown_brief_brand(self):
        slot = _shot("模版", "品牌logo入画")
        variant = _shot(
            "创意库",
            "LINSY HOME 品牌logo穿越进入主会场",
            source_brand="林氏家居",
        )
        rows, _ = generate_template_rows({
            "项目名称": "星河科技发布会",
            "品牌或客户": "星河科技",
            "视频时长": "60s",
        }, [TemplateDecision(slot, variant, "REPLACE", 90, "功能匹配")])

        self.assertNotIn("LINSY", rows[0]["画面描述"])
        self.assertIn("星河科技", rows[0]["画面描述"])

    def test_candidate_without_brand_provenance_is_not_adopted(self):
        slot = _shot("模版", "品牌logo入画")
        variant = _shot("创意库", "主视觉logo穿越进入主会场")

        decision = match_template_slots(_brief(), [slot], [variant])[0]

        self.assertEqual(decision.mode, "KEEP")
        self.assertIsNone(decision.variant)
        self.assertIn("缺少来源品牌声明", decision.gap_note)

    def test_brand_neutral_requires_source_field_or_human_confirmation(self):
        slot = _shot("模版", "品牌logo入画")
        unverified = _shot(
            "创意库",
            "主视觉logo穿越进入主会场",
            brand_neutral=True,
        )
        verified = _shot(
            "创意库",
            "主视觉logo穿越进入主会场",
            source_row=8,
            brand_neutral=True,
            brand_neutral_evidence="人工确认:reviewer-a",
        )

        rejected = match_template_slots(_brief(), [slot], [unverified])[0]
        adopted = match_template_slots(_brief(), [slot], [verified])[0]

        self.assertEqual(rejected.mode, "KEEP")
        self.assertIn("品牌中性缺少明确源字段或人工确认", rejected.gap_note)
        self.assertIn(adopted.mode, {"REPLACE", "ADAPT"})
        self.assertEqual(adopted.variant.source_row, 8)

    def test_explicit_unknown_source_brand_is_removed_without_brand_cue(self):
        slot = _shot("模版", "模板主会场")
        variant = _shot(
            "创意库",
            "另一科技发布会主会场全景",
            source_brand="另一科技",
        )
        decision = TemplateDecision(slot, variant, "REPLACE", 90, "功能匹配")

        rows, details = generate_template_rows(_brief(), [decision])

        self.assertNotIn("另一科技", rows[0]["画面描述"])
        self.assertIn("星河科技", rows[0]["画面描述"])
        self.assertEqual(details[0]["采用来源品牌"], "另一科技")

    def test_generator_falls_back_to_keep_when_invalid_decision_is_injected(self):
        slot = _shot("模版", "模板品牌logo入画")
        variant = _shot("创意库", "未声明来源的logo穿越")
        decision = TemplateDecision(slot, variant, "REPLACE", 90, "功能匹配")
        baseline = TemplateDecision(slot, None, "KEEP", 0, "保留模板")

        rows, details = generate_template_rows(_brief(), [decision])
        baseline_rows, _ = generate_template_rows(_brief(), [baseline])

        self.assertEqual(rows[0]["画面描述"], baseline_rows[0]["画面描述"])
        self.assertEqual(details[0]["替换方式"], "KEEP")
        self.assertIn("缺少来源品牌声明", details[0]["缺口说明"])

    def test_audit_rejects_injected_replace_without_brand_provenance(self):
        slot = _shot("模版", "模板品牌logo入画")
        variant = _shot("创意库", "未声明来源的logo穿越")
        decision = TemplateDecision(slot, variant, "REPLACE", 90, "功能匹配")
        rows, details = generate_template_rows(_brief(), [decision])

        result = audit_generation(_brief(), [slot], [decision], rows, details)

        self.assertIn("missing_source_brand_provenance", result["blocking_items"])

    def test_loader_does_not_trust_brand_neutral_without_human_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "M+发布会类通用脚本库.xlsx"
            wb = Workbook()
            ws = wb.active
            ws.title = "创意库"
            headers = ["结构", "镜号", "画面描述", "来源品牌", "品牌中性"]
            for col, header in enumerate(headers, start=1):
                ws.cell(5, col, header)
            ws.append([])
            ws.cell(7, 1, "开场造势")
            ws.cell(7, 2, "1")
            ws.cell(7, 3, "品牌logo入画")
            ws.cell(7, 4, "星河科技")
            ws.cell(8, 1, "开场造势")
            ws.cell(8, 2, "2")
            ws.cell(8, 3, "通用主会场空镜")
            ws.cell(8, 5, "是")
            ws.cell(9, 1, "开场造势")
            ws.cell(9, 2, "3")
            ws.cell(9, 3, "没有声明的素材")
            wb.save(path)
            wb.close()

            variants = load_creative_variants(root)

        self.assertEqual(variants[0].source_brand, "星河科技")
        self.assertFalse(variants[0].brand_neutral)
        self.assertTrue(variants[1].brand_neutral)
        self.assertEqual(variants[1].brand_neutral_evidence, "")
        decision = match_template_slots(_brief(), [_shot("模版", "通用主会场空镜")], [variants[1]])[0]
        self.assertEqual(decision.mode, "KEEP")
        self.assertIn("人工确认", decision.gap_note)
        self.assertFalse(variants[2].brand_neutral)
        self.assertEqual(variants[2].brand_neutral_evidence, "")

    def test_keep_template_does_not_require_source_brand_provenance(self):
        slot = _shot("模版", "模板品牌logo入画")
        decision = TemplateDecision(slot, None, "KEEP", 0, "保留模板")

        rows, details = generate_template_rows(_brief(), [decision])

        self.assertTrue(rows[0]["画面描述"])
        self.assertEqual(details[0]["替换方式"], "KEEP")

    def test_unknown_source_brand_cue_is_adapted_without_static_alias(self):
        self.assertEqual(
            adapt_source_text_to_project("另一科技 品牌logo入画", "星河科技"),
            "星河科技 品牌logo入画",
        )
        self.assertEqual(
            unknown_brand_mentions("星河科技", "另一科技 品牌logo入画"),
            ["另一科技"],
        )
        self.assertEqual(
            adapt_source_text_to_project("另一科技发布会现场", "星河科技", source_brand="另一科技"),
            "星河科技发布会现场",
        )

    def test_multiple_source_brands_are_individually_adapted(self):
        adapted = adapt_source_text_to_project(
            "XTransfer 与另一科技联合发布会现场",
            "星河科技",
            source_brand="XTransfer；另一科技",
        )

        self.assertNotIn("XTransfer", adapted)
        self.assertNotIn("另一科技", adapted)
        self.assertIn("星河科技", adapted)

    def test_bilingual_source_brand_alias_is_adapted(self):
        adapted = adapt_source_text_to_project(
            "喜力F1主题区与Heineken品牌墙",
            "逐峰运动",
            source_brand="Heineken；F1",
        )

        self.assertNotIn("喜力", adapted)
        self.assertNotIn("Heineken", adapted)
        self.assertNotIn("F1", adapted)
        self.assertIn("逐峰运动", adapted)

    def test_explicit_source_brand_does_not_consume_surrounding_action_phrase(self):
        adapted = adapt_source_text_to_project(
            "夕阳穿过ZEISS舞台桁架和品牌标识，人物形成剪影",
            "星澜集团",
            source_brand="ZEISS",
        )

        self.assertEqual(
            adapted,
            "夕阳穿过星澜集团舞台桁架和品牌标识，人物形成剪影",
        )

    def test_multiple_source_brands_detect_any_residual_leak(self):
        self.assertTrue(source_brand_leaks(
            "美的发布会",
            "XTransfer；另一科技",
            "另一科技发布会现场",
        ))
        self.assertFalse(source_brand_leaks(
            "美的发布会",
            "XTransfer；另一科技",
            "美的发布会现场",
        ))

    def test_audit_blocks_residual_from_any_multiple_source_brand(self):
        slot = _shot("模版", "模板主会场")
        variant = _shot(
            "创意库",
            "XTransfer 与另一科技联合发布会现场",
            source_brand="XTransfer；另一科技",
        )
        decision = TemplateDecision(slot, variant, "REPLACE", 90, "功能匹配")
        rows, details = generate_template_rows(_brief(), [decision])
        rows[0]["画面描述"] = "另一科技发布会现场"

        result = audit_generation(_brief(), [slot], [decision], rows, details)

        self.assertIn("source_brand_leak", result["blocking_items"])

    def test_unknown_project_rejects_ocr_without_structured_brand_verdict(self):
        self.assertTrue(brand_evidence_unverified("星河科技", [], "另一科技"))
        self.assertFalse(brand_evidence_unverified("星河科技", ["星河科技"], "星河科技"))
        reason = _brand_conflict_reason(
            {"品牌或客户": "星河科技"},
            {"caption": "品牌墙", "tags": ["发布会"], "ocr_text": "另一科技", "brand_names": []},
        )
        self.assertIn("品牌识别结论", reason)

    def test_empty_brand_verdict_is_not_treated_as_brand_neutral(self):
        reason = _brand_conflict_reason(
            {"品牌或客户": "美的"},
            {"caption": "发布会主会场", "tags": ["主会场"], "ocr_text": "", "brand_names": []},
        )
        self.assertIn("缺少品牌识别结论", reason)

    def test_explicit_no_brand_verdict_can_use_neutral_visual(self):
        self.assertEqual(
            _brand_conflict_reason(
                {"品牌或客户": "美的"},
                {"caption": "发布会主会场", "tags": ["主会场"], "ocr_text": "", "brand_names": ["无"]},
            ),
            "",
        )

    def test_uncertain_brand_verdict_is_blocked(self):
        reason = _brand_conflict_reason(
            {"品牌或客户": "美的"},
            {"caption": "发布会主会场", "tags": ["主会场"], "ocr_text": "", "brand_names": ["未知"]},
        )
        self.assertIn("不确定", reason)

    def test_mixed_uncertain_brand_verdict_is_blocked(self):
        for names in (["未知", "无"], ["未知", "美的"]):
            with self.subTest(names=names):
                reason = _brand_conflict_reason(
                    {"品牌或客户": "美的"},
                    {"caption": "发布会主会场", "tags": ["主会场"], "ocr_text": "", "brand_names": names},
                )
                self.assertIn("不确定", reason)

    def test_theme_word_evidence_is_generic_instead_of_project_specific(self):
        self.assertTrue(_required_term_present("星河计划主题字样", "大屏出现星河计划"))
        self.assertFalse(_required_term_present("星河计划主题字样", "大屏出现其他主题"))
        audit_source = Path("mvp_v2/audit.py").read_text(encoding="utf-8")
        self.assertNotIn("智美万象", audit_source)

    def test_unknown_brand_draft_has_no_internal_or_placeholder_language(self):
        brief = _brief()
        brief["必须出现的内容"] = "主题装置空镜"
        slot = _shot("模版", "主会场空镜", slot_index=4, shot_no="4")
        rows, _ = generate_template_rows(
            brief,
            [TemplateDecision(slot, None, "KEEP", 0, "保留模板")],
        )

        visible = rows[0]["画面描述"]
        self.assertNotIn("Brief要求", visible)
        self.assertNotIn("核心内容", visible)
        self.assertIn("主题装置空镜", visible)

    def test_exhibition_presentation_uses_brief_topic_instead_of_placeholder(self):
        brief = {
            "项目名称": "星澜循环材料未来体验展",
            "品牌或客户": "星澜循环材料",
            "脚本类型": "展会",
            "视频时长": "5s",
            "必须出现的内容": "主题论坛",
        }
        slot = _shot(
            "模版",
            "发布舞台与满场观众正反打，高管宣讲或新品揭幕",
            script_type="展会",
            shot_function="高管宣讲与发布",
            visual_subject="舞台、观众、品牌物料",
            duration_seconds="5",
            slot_index=1,
            shot_no="1",
        )

        rows, _ = generate_template_rows(
            brief,
            [TemplateDecision(slot, None, "KEEP", 0, "保留模板")],
        )

        visible = rows[0]["画面描述"]
        self.assertIn("主题论坛", visible)
        self.assertIn("具体议题按确认版本呈现", visible)
        self.assertNotIn("核心内容", visible)


def _brief() -> dict:
    return {
        "项目名称": "星河科技发布会",
        "品牌或客户": "星河科技",
        "脚本类型": "发布会",
        "视频时长": "60s",
        "风格调性": "科技感",
        "必须出现的内容": "品牌logo",
        "禁忌或不能出现的内容": "竞品",
    }


def _shot(sheet: str, description: str, **overrides) -> SourceShot:
    values = dict(
        script_type="发布会",
        source_file="library.xlsx",
        source_sheet=sheet,
        source_row=7,
        shot_no="1",
        structure="开场造势",
        effect="字体穿梭",
        shot_size="全景",
        camera_move="前推",
        visual_desc=description,
        reference_image_path="library.xlsx#创意库!R7C4",
        slot_index=1,
    )
    values.update(overrides)
    return SourceShot(
        **values,
    )


if __name__ == "__main__":
    unittest.main()
