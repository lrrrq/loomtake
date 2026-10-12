import unittest

from mvp_v2.image_retrieval import search_images_for_slot
from mvp_v2.visual_assets import _score_visual_match


class ImageMatchUsabilityTests(unittest.TestCase):
    def test_concert_is_not_itself_evidence_or_requirement_of_singing(self):
        stage = _score_visual_match(
            "演唱会舞台灯光空镜", "灯光照亮空舞台", ["舞台", "灯光"],
            apply_launch_conflicts=False, apply_brand_conflicts=False,
        )
        self.assertNotIn("合唱/演唱", stage["missing"])
        action = _score_visual_match(
            "明星演唱，观众合唱", "演唱会现场舞台与观众全景", ["演唱会", "观众"],
            apply_launch_conflicts=False, apply_brand_conflicts=False,
        )
        self.assertIn("合唱/演唱", action["missing"])

    def test_blurred_foreground_does_not_hide_clear_building_subject(self):
        for caption in (
            "虚化的树叶位于前景，清晰的商业体入口建筑与广场占据画面中央",
            "一只飞鸟掠过，背景是树叶；清晰的商业体入口建筑与广场占据画面中央",
        ):
            with self.subTest(caption=caption):
                match = _score_visual_match(
                    "商业体入口建筑与广场", caption, ["建筑"],
                    apply_launch_conflicts=False, apply_brand_conflicts=False,
                )
                self.assertEqual(match["conflicts"], [])

    def test_launch_adaptation_keeps_explicit_aerial_and_singing_requirements(self):
        from tests.test_mvp_v2_empty_visual_regression import BRIEF, _analysis, _manifest
        for slot_index, query, caption, tags, required in [
            (5, "航拍签到墙顾客互动", "发布会现场，观众在签到台登记资料并与工作人员互动。",
             ["签到", "登记", "顾客", "互动"], "航拍/俯视"),
            (15, "观众合唱颁发仪式", "发布会现场，舞台上四位嘉宾站在一起，背景为品牌大屏与正式舞台灯光。",
             ["发布会", "舞台", "嘉宾", "品牌活动", "LED屏"], "合唱/演唱"),
        ]:
            with self.subTest(slot_index=slot_index):
                result = search_images_for_slot(
                    {"slot_index": slot_index, "画面描述": query}, BRIEF,
                    [_manifest("reference", "reference.png")],
                    [_analysis("reference", caption, tags, people_count=4)],
                )
                self.assertNotEqual(result["status"], "auto")
                self.assertTrue(result["candidates"])
                self.assertIn(required, result["candidates"][0]["missing"])

    def test_blurred_building_behind_bird_is_not_building_subject(self):
        result = _score_visual_match(
            "航拍商业体外观，展现广场与入口建筑",
            "一只灰鹭在模糊的城市建筑前展翅飞翔",
            ["建筑", "城市", "飞鸟"],
            apply_launch_conflicts=False,
            apply_brand_conflicts=False,
        )
        self.assertLess(result["score"], 80)
        self.assertIn("主体要求“外场/建筑”仅出现在背景", result["conflicts"])

    def test_relevant_aerial_outweighs_same_source_ground_reference(self):
        captions = {
            "bird": "一只灰鹭在模糊的城市建筑前展翅飞翔",
            "ground": "地面远景拍摄商业体外观，入口和广场完整入画",
            "aerial": "航拍商业体外观，俯瞰开阔广场与入口建筑",
        }
        manifest = [{
            "image_id": key, "原图路径": f"/anonymous/{key}.png",
            "asset_role": "source_reference", "来源文件": "匿名活动库.xlsx",
            "来源 sheet": "模版" if key != "aerial" else "创意库",
            "来源行": 7 if key != "aerial" else 30,
        } for key in captions]
        result = search_images_for_slot(
            {"slot_index": 1, "画面描述": "航拍商业体外观，展现广场与入口建筑",
             "采用来源文件": "匿名活动库.xlsx", "采用来源 sheet": "模版", "采用来源行": 7},
            {"脚本类型": "明星活动"}, manifest,
            [{"image_id": key, "caption": caption, "tags": ["建筑"],
              "brand_names": ["无"], "source": "fixture"} for key, caption in captions.items()],
        )
        self.assertEqual(result["candidates"][0]["image_id"], "aerial")
        ground = next(item for item in result["candidates"] if item["image_id"] == "ground")
        self.assertLess(ground["score"], 80)
        self.assertIn("航拍/俯视", ground["missing"])
        self.assertTrue(any(item["image_id"] == "bird" for item in result["rejected"]))

    def test_singing_action_is_not_satisfied_by_product_with_crowd_background(self):
        result = _score_visual_match(
            "全场粉丝合唱，明星在舞台领唱",
            "饮品展台上的产品特写，背景为活动观众",
            ["观众", "活动"], apply_launch_conflicts=False, apply_brand_conflicts=False,
        )
        self.assertLess(result["score"], 80)
        self.assertIn("合唱/演唱", result["missing"])

    def test_building_as_foreground_is_not_rejected_for_background_bird(self):
        result = _score_visual_match(
            "航拍商业体入口建筑与广场",
            "航拍商业体入口建筑与广场，背景中有飞鸟掠过",
            [], apply_launch_conflicts=False, apply_brand_conflicts=False,
        )
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["score"], 100)

    def test_background_roles_preserve_bird_and_billboard_compositions(self):
        for description, observations in (
            ("飞鸟展开翅膀，背景为建筑", {"main_subject": "飞鸟", "background_subjects": ["建筑"]}),
            ("城市建筑慢门掠过，一只飞鸟横穿画面", {"main_subject": "飞鸟", "background_subjects": ["建筑"]}),
            ("建筑外观与外立面广告牌", {"main_subject": "户外广告牌", "background_subjects": ["建筑"]}),
        ):
            with self.subTest(description=description):
                result = _score_visual_match(
                    description, "飞鸟和广告牌可见，背景是城市建筑", ["建筑"],
                    observations=observations, apply_launch_conflicts=False, apply_brand_conflicts=False,
                )
                self.assertNotIn("主体要求“外场/建筑”仅出现在背景", result["conflicts"])

    def test_explicit_building_subject_is_not_satisfied_by_a_requested_background(self):
        result = _score_visual_match(
            "建筑外观特写，背景为飞鸟", "飞鸟展翅，模糊建筑位于背景", ["建筑"],
            observations={"main_subject": "飞鸟", "background_subjects": ["建筑"]},
            apply_launch_conflicts=False, apply_brand_conflicts=False,
        )
        self.assertIn("主体要求“外场/建筑”仅出现在背景", result["conflicts"])

    def test_explicit_outdoor_screen_does_not_imply_an_indoor_stage(self):
        caption = "建筑外立面户外LED大屏播放活动影像"
        for query in ("户外大屏播放活动影像", "广场LED大屏播放明星影像"):
            with self.subTest(query=query):
                match = _score_visual_match(query, caption, ["大屏"], apply_launch_conflicts=False)
                self.assertEqual(match["conflicts"], [])
                self.assertEqual(match["missing"], [])
        indoor = _score_visual_match("室内主会场舞台大屏", caption, ["大屏"], apply_launch_conflicts=False)
        self.assertIn("需要内场舞台/大屏但画面是外场建筑", indoor["conflicts"])
        absent = _score_visual_match("户外大屏播放活动影像", "户外建筑外观", ["建筑"], apply_launch_conflicts=False)
        self.assertIn("主会场/大屏", absent["missing"])

    def test_postposed_background_does_not_override_an_explicit_subject(self):
        for subject in ("飞鸟", "广告牌"):
            for suffix in ("为背景", "作为背景"):
                with self.subTest(subject=subject, suffix=suffix):
                    result = _score_visual_match(
                        f"建筑为主体，{subject}{suffix}", f"{subject}是主体，远处是模糊建筑", ["建筑"],
                        observations={"main_subject": subject, "background_subjects": ["建筑"]},
                        apply_launch_conflicts=False, apply_brand_conflicts=False,
                    )
                    self.assertIn("主体要求“外场/建筑”仅出现在背景", result["conflicts"])

    def test_legacy_distant_building_caption_keeps_subject_roles(self):
        caption = "画面中一只鸟展翅飞翔于粉色天空前，远处是一栋模糊的高层建筑，呈现出柔和的都市剪影氛围。"
        building = _score_visual_match("匿名广场全景，晨光洒落，人流渐聚，建筑轮廓清晰", caption, ["建筑", "飞鸟"], apply_launch_conflicts=False, apply_brand_conflicts=False)
        self.assertIn("主体要求“外场/建筑”仅出现在背景", building["conflicts"])
        composition = _score_visual_match("城市建筑慢门掠过，一只飞鸟横穿画面", caption, ["建筑", "飞鸟"], apply_launch_conflicts=False, apply_brand_conflicts=False)
        self.assertEqual(composition["conflicts"], [])
