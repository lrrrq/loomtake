from copy import deepcopy
import unittest

from mvp_v2.formal_image_candidates import allocate_diverse_defaults
from mvp_v2.formal_image_workspace import formal_image_search_slot
from mvp_v2.image_retrieval import _slot_required_terms, search_images_for_slot
from mvp_v2.visual_assets import _score_visual_match


class CurrentProductImageMatchingTests(unittest.TestCase):
    """A framing/action word cannot establish the requested physical product."""

    @staticmethod
    def _analysis(key, caption, **observations):
        return {
            "image_id": key, "caption": caption, "tags": ["特写"],
            "brand_names": ["无"], "quality_flags": [], "negative_flags": [],
            **observations,
        }

    def _search(self, description, observations, *, detail=None):
        row = {"镜号": "2", "画面描述": description}
        original = deepcopy(row)
        slot = formal_image_search_slot(row=row, detail=detail or {}, planned={}, fallback_index=2)
        result = search_images_for_slot(
            slot, {"脚本类型": "发布会", "品牌或客户": "无"},
            [{"image_id": item["image_id"], "asset_role": "source_reference"} for item in observations],
            observations, top_k=None,
        )
        self.assertEqual(row, original)
        return result

    def _assert_reference(self, candidate):
        self.assertEqual(candidate["match_tier"], "reference")
        self.assertFalse(candidate["default_eligible"])
        self.assertTrue(candidate["requires_human_review"])
        self.assertTrue(candidate["missing"] or candidate["conflicts"])

    def test_three_wrong_subject_action_structures_remain_browseable_without_auto_selection(self):
        examples = (
            ("同一台匿名RC-410电饭煲外观特写，展示机身正面与侧面。",
             "一个人横握手机拍摄发布会，手机屏幕上出现匿名品牌标识。", "拍摄记录"),
            ("同一台匿名RC-410电饭煲上盖控制面板特写，手指按键操作。",
             "一只手在车内大屏上操作智能家居界面，屏幕显示灯带设备。", "触控操作"),
            ("从同一台匿名RC-410电饭煲盛饭，端至相邻餐桌。",
             "发布会现场一位参会者拿着饮料杯，前景桌上有小食。", "手持饮料"),
        )
        ranked = {}
        for index, (description, caption, action) in enumerate(examples, start=1):
            with self.subTest(index=index):
                result = self._search(description, [self._analysis(
                    f"wrong-{index}", caption, action_type=action,
                    tags=["特写", "上手", "餐桌", "茶歇"],
                )])
                self.assertEqual(len(result["candidates"]), 1)
                self._assert_reference(result["candidates"][0])
                ranked[str(index)] = result
        self.assertEqual(allocate_diverse_defaults(ranked)["selected_by_shot"], {})

    def test_matching_physical_product_model_and_action_are_eligible(self):
        examples = (
            ("厨房台面上的RC-410电饭煲外观特写。", "厨房台面上的RC-410电饭煲机身特写。", "静置"),
            ("RC-410电饭煲上盖控制面板特写，手指按键操作。",
             "RC-410电饭煲上盖控制面板特写，一根手指按下按键。", "按键操作"),
            ("把准备好的米放入RC-410电饭煲内锅。", "双手把米倒入RC-410电饭煲的内锅。", "放米入锅"),
            ("RC-410电饭煲成饭开盖，米饭清楚可见。", "手打开RC-410电饭煲上盖，内锅米饭可见。", "开盖"),
            ("从RC-410电饭煲盛饭并端至餐桌。", "从RC-410电饭煲把米饭盛入碗，再端到餐桌。", "盛饭上桌"),
        )
        for index, (description, caption, action) in enumerate(examples):
            with self.subTest(index=index):
                candidate = self._search(description, [self._analysis(
                    "right", caption, main_subject="RC-410电饭煲", action_type=action,
                    foreground_subjects=["电饭煲", "米饭"],
                )])["candidates"][0]
                self.assertTrue(candidate["default_eligible"], candidate)
                self.assertEqual(candidate["missing"], [])
                self.assertEqual(candidate["conflicts"], [])

    def test_product_tags_and_ocr_cannot_replace_a_visible_product(self):
        for caption, observations in (
            ("手机屏幕特写，人物正在拍摄。", {"tags": ["电饭煲", "RC-410", "特写"]}),
            ("大屏幕特写，屏幕显示产品名称。", {"ocr_text": "RC-410 电饭煲"}),
            ("屏幕展示RC-410电饭煲宣传图片，现场只有大屏幕。", {}),
        ):
            with self.subTest(caption=caption):
                candidate = self._search("RC-410电饭煲外观特写。", [
                    self._analysis("screen", caption, **observations),
                ])["candidates"][0]
                self._assert_reference(candidate)

    def test_positive_structured_subjects_can_establish_physical_product(self):
        for field in ("subjects", "foreground_subjects", "main_subject"):
            with self.subTest(field=field):
                value = "RC-410电饭煲" if field == "main_subject" else ["RC-410电饭煲"]
                candidate = self._search("RC-410电饭煲外观特写。", [
                    self._analysis("physical", "银色机身特写，轮廓完整。", **{field: value}),
                ])["candidates"][0]
                self.assertTrue(candidate["default_eligible"], candidate)

    def test_model_in_ocr_requires_the_actual_product_subject(self):
        candidates = self._search("RC-410电饭煲外观特写。", [
            self._analysis("physical", "电饭煲机身特写。", main_subject="电饭煲", ocr_text="RC-410"),
            self._analysis("screen", "车内屏幕特写。", main_subject="车内屏幕", ocr_text="RC-410 电饭煲"),
        ])["candidates"]
        by_id = {item["image_id"]: item for item in candidates}
        self.assertTrue(by_id["physical"]["default_eligible"], by_id["physical"])
        self._assert_reference(by_id["screen"])

    def test_unknown_and_different_models_do_not_auto_match(self):
        result = self._search("RC-410电饭煲外观特写。", [
            self._analysis("unknown", "电饭煲机身特写，型号文字无法识别。", main_subject="电饭煲"),
            self._analysis("different", "RC-411电饭煲机身特写。", main_subject="RC-411电饭煲"),
            self._analysis("longer-number", "RC-4100电饭煲机身特写。", main_subject="RC-4100电饭煲"),
            self._analysis("longer-suffix", "RC-410-Pro电饭煲机身特写。", main_subject="RC-410-Pro电饭煲"),
        ])
        for candidate in result["candidates"]:
            with self.subTest(image_id=candidate["image_id"]):
                self._assert_reference(candidate)

    def test_background_product_does_not_replace_foreground_but_explicit_background_is_allowed(self):
        observation = self._analysis(
            "depth", "前景手持饭勺把米饭盛进白瓷碗，背景RC-410电饭煲清楚可见。",
            main_subject="盛饭的手和白瓷碗", foreground_subjects=["手", "饭勺", "米饭", "白瓷碗"],
            background_subjects=["RC-410电饭煲"], action_type="盛饭", tags=["特写"],
        )
        foreground = self._search("RC-410电饭煲外观特写。", [observation])["candidates"][0]
        self._assert_reference(foreground)
        background = self._search(
            "前景手持饭勺把米饭盛进白瓷碗，背景RC-410电饭煲可见。", [observation],
        )["candidates"][0]
        self.assertTrue(background["default_eligible"], background)

    def test_negated_product_or_action_does_not_count_as_positive_evidence(self):
        for description, caption in (
            ("电饭煲外观特写。", "特写画面只有玻璃杯，台面没有电饭煲。"),
            ("电饭煲外观特写。", "未见电饭煲，画面是手机特写。"),
            ("把米放入电饭煲内锅。", "电饭煲静置，画面没有放米入锅动作。"),
        ):
            with self.subTest(caption=caption):
                self._assert_reference(self._search(description, [
                    self._analysis("negative", caption),
                ])["candidates"][0])

    def test_preparation_raw_rice_and_carrying_elsewhere_do_not_cover_specific_steps(self):
        cases = (
            ("备米放入RC-410电饭煲内锅。",
             "备米操作特写，米洗好后仍在白瓷碗中，RC-410电饭煲放在旁边。"),
            ("RC-410电饭煲开盖成饭。",
             "RC-410电饭煲上盖打开，内锅米饭尚未煮熟。"),
            ("从RC-410电饭煲盛饭并端至餐桌。",
             "从RC-410电饭煲盛饭，手把饭碗端到洗菜池，餐桌在远处。"),
        )
        for description, caption in cases:
            with self.subTest(caption=caption):
                self._assert_reference(self._search(description, [
                    self._analysis("other-step", caption),
                ])["candidates"][0])

    def test_kitchen_requirement_is_not_satisfied_by_generic_indoor_scene(self):
        result = self._search("厨房台面上的电饭煲特写。", [
            self._analysis("room", "卧室桌上的电饭煲特写。", scene="室内"),
            self._analysis("kitchen", "明亮厨房台面上的电饭煲特写。", scene="厨房"),
        ])
        by_id = {item["image_id"]: item for item in result["candidates"]}
        self._assert_reference(by_id["room"])
        self.assertTrue(by_id["kitchen"]["default_eligible"], by_id["kitchen"])

    def test_current_visual_drops_old_gap_bonus_and_keeps_explicit_requirement(self):
        detail = {
            "缺口说明": "Brief规划内容：匿名旧要求；旧模板来源保留",
            "required_brief_content": ["匿名当前要求"],
        }
        before = deepcopy(detail)
        current = formal_image_search_slot(
            row={"镜号": "4", "画面描述": "电饭煲上盖面板特写。"},
            detail=detail, planned={}, fallback_index=4,
        )
        self.assertEqual(_slot_required_terms(current), ["匿名当前要求"])
        self.assertIn("匿名旧要求", current["缺口说明"])
        self.assertEqual(detail, before)
        self.assertEqual(_slot_required_terms(detail), ["匿名当前要求", "匿名旧要求"])

    def test_legacy_branch_and_existing_nonproduct_intent_are_unchanged(self):
        legacy = _score_visual_match(
            "电饭煲外观特写。", "手机拍摄活动的特写。", ["特写"],
            apply_launch_conflicts=False, apply_brand_conflicts=False,
            current_shot_authority=False,
        )
        self.assertNotIn("电饭煲", legacy["missing"])
        self.assertEqual(legacy["requirements"], [])
        current = self._search("低机位仰拍歌手与圆形屏。", [
            self._analysis("singer", "低机位仰拍歌手演唱，身后圆形屏清晰。"),
        ])["candidates"][0]
        self.assertTrue(current["default_eligible"], current)


if __name__ == "__main__":
    unittest.main()
