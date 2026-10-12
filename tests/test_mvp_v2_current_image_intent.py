import unittest

from mvp_v2.formal_image_workspace import formal_image_search_slot
from mvp_v2.formal_image_candidates import allocate_diverse_defaults, search_package_library_candidates
from mvp_v2.image_retrieval import search_images_for_slot


class CurrentImageIntentTests(unittest.TestCase):
    def test_stage_action_does_not_auto_match_refreshment_crowd(self):
        slot = formal_image_search_slot(row={"镜号": "5", "画面描述":
            "地面综合机位，台上人物完成环节流程动作，台下人群注视，正式节点在掌声中推进。"}, detail={}, planned={}, fallback_index=5)
        result = search_images_for_slot(slot, {"脚本类型": "发布会"},
            [{"image_id": key, "asset_role": "source_reference"} for key in ("bar", "stage")],
            [{"image_id": "bar", "caption": "发布会现场人群聚集，工作人员在吧台忙碌，参会者交流参观", "tags": ["人群", "嘉宾"], "brand_names": ["无"]},
             {"image_id": "stage", "caption": "嘉宾站在舞台上完成启动动作，台下观众注视鼓掌", "tags": ["人群", "嘉宾"], "brand_names": ["无"]}], top_k=None)
        candidates = {c["image_id"]: c for c in result["candidates"]}
        self.assertTrue(candidates["stage"]["default_eligible"])
        self.assertFalse(candidates["bar"]["default_eligible"])
        self.assertIn("台上人物与动作", candidates["bar"]["missing"])
        self.assertTrue(candidates["bar"]["conflicts"])

    def test_pullback_uses_wide_evidence_without_inventing_360(self):
        slot = formal_image_search_slot(row={"镜号": "16", "画面描述":
            "广角拉远呈现会场整体空间，产品清晰可见"}, detail={}, planned={}, fallback_index=16)
        result = search_images_for_slot(slot, {"脚本类型": "发布会"},
            [{"image_id": key, "asset_role": "source_reference"} for key in ("wide", "close")],
            [{"image_id": key, "caption": "发布会现场，产品展台与观众席", "tags": ["产品"], "shot_type": view, "brand_names": ["无"]}
             for key, view in (("wide", "wide_shot"), ("close", "close_up"))], top_k=None)
        candidates = {c["image_id"]: c for c in result["candidates"]}
        self.assertTrue(candidates["wide"]["default_eligible"])
        self.assertFalse(candidates["close"]["default_eligible"])
        self.assertNotIn("360/会场全貌", candidates["wide"]["missing"])
        self.assertTrue(candidates["wide"]["reference_notes"])

    def test_product_event_title_is_not_visible_product_evidence(self):
        slot = formal_image_search_slot(row={"镜号": "2", "画面描述": "会场内产品清晰可见"}, detail={}, planned={}, fallback_index=2)
        result = search_images_for_slot(slot, {"脚本类型": "发布会"}, [{"image_id": "talk", "asset_role": "source_reference"}],
            [{"image_id": "talk", "caption": "产品发布会现场，男士正在演讲，大屏显示产品宣传文字", "tags": ["产品发布会"], "brand_names": ["无"]}], top_k=None)
        self.assertFalse(result["candidates"][0]["default_eligible"])
        self.assertIn("产品/展品", result["candidates"][0]["missing"])

    def test_product_screen_and_local_booth_are_not_physical_product_or_venue_overview(self):
        cases = [
            ('会场中产品清晰可见', '演讲者站在舞台前，屏幕展示智能产品界面和产品发布信息', ['产品/展品']),
            ('会场整体空间全景', '室内发布会场景，中央有一个卡通吉祥物站在展台上，两侧展示家电', ['会场全貌']),
            ('穿过会场通道，舞台逐渐入画', '人物站在舞台中央，背景大屏幕播放主视觉', ['空间通道']),
            ('会场整体空间，产品清晰可见', '室内发布会现场，众多观众聚集在展厅中，专注观看产品展示，远处有工作人员讲解', ['产品/展品', '会场全貌']),
        ]
        for description, caption, missing in cases:
            with self.subTest(description=description):
                slot = formal_image_search_slot(row={'镜号': '1', '画面描述': description}, detail={}, planned={}, fallback_index=1)
                result = search_images_for_slot(slot, {'脚本类型': '发布会'}, [{'image_id': 'reference', 'asset_role': 'source_reference'}],
                    [{'image_id': 'reference', 'caption': caption, 'tags': ['发布会'], 'shot_type': 'wide_shot', 'brand_names': ['无']}], top_k=None)
                self.assertFalse(result['candidates'][0]['default_eligible'])
                self.assertTrue(set(missing).issubset(result['candidates'][0]['missing']))

    def test_venue_aerial_keeps_scene_evidence_and_does_not_default_to_city_skyline(self):
        assets = [{"image_id": key, "asset_role": "source_reference"}
                  for key in ("city", "venue", "ground")]
        analysis = [
            {"image_id": "city", "caption": "城市天际线日出航拍全景", "tags": ["发布会", "航拍"], "brand_names": ["无"]},
            {"image_id": "venue", "caption": "航拍会场上方，舞台与观众席清晰可见", "tags": ["航拍"], "brand_names": ["无"]},
            {"image_id": "ground", "caption": "地面机位会场全景，舞台与观众席清晰可见", "tags": ["会场"], "brand_names": ["无"]},
        ]
        for category in ("发布会", "音乐节", "展会"):
            with self.subTest(category=category):
                slot = formal_image_search_slot(row={"镜号": "3", "画面描述": "航拍会场上方，品牌视觉元素掠过画面"},
                                                detail={}, planned={}, fallback_index=3)
                result = search_images_for_slot(slot, {"脚本类型": category}, assets, analysis, top_k=8)
                by_id = {item["image_id"]: item for item in result["candidates"]}
                self.assertTrue(by_id["venue"]["default_eligible"])
                self.assertFalse(by_id["city"]["default_eligible"])
                self.assertFalse(by_id["ground"]["default_eligible"])
                self.assertEqual(result["candidates"][0]["image_id"], "venue")
                self.assertIn("会场/活动现场", by_id["city"]["missing"])

    def test_crowd_timelapse_does_not_invent_a_construction_requirement(self):
        assets = [{"image_id": key, "asset_role": "source_reference"} for key in ("crowd", "construction")]
        analysis = [
            {"image_id": "crowd", "caption": "会场通道人群快速流动，观众进出展区", "tags": ["人群", "会场"], "brand_names": ["无"]},
            {"image_id": "construction", "caption": "会场内工人正在搭建施工", "tags": ["施工"], "brand_names": ["无"]},
        ]
        for query, expected in (("地面延时捕捉会场通道的人流快速流动", "crowd"),
                                ("会场内工人搭建施工延时", "construction")):
            with self.subTest(query=query):
                slot = formal_image_search_slot(row={"镜号": "9", "画面描述": query},
                                                detail={}, planned={}, fallback_index=9)
                result = search_images_for_slot(slot, {"脚本类型": "发布会"}, assets, analysis, top_k=8)
                by_id = {item["image_id"]: item for item in result["candidates"]}
                self.assertTrue(by_id[expected]["default_eligible"])
                self.assertEqual(result["candidates"][0]["image_id"], expected)
                if expected == "crowd":
                    self.assertNotIn("搭建/施工延时", by_id[expected]["missing"])
                else:
                    self.assertFalse(by_id["crowd"]["default_eligible"])

    def test_current_people_scene_outweighs_old_brand_slot_and_source(self):
        slot = formal_image_search_slot(
            row={"镜号": "2", "画面描述": "外场经销商代表在展板前拍照打卡，现场人流涌动"},
            detail={"逻辑槽位": 1, "槽位功能": "品牌开场识别", "必须视觉证据": "logo",
                    "采用来源文件": "anonymous.xlsx", "采用来源 sheet": "模版", "采用来源行": 7},
            planned={"primary_event": "代表拍照打卡"}, fallback_index=2,
        )
        manifest = [{"image_id": name, "asset_role": "source_reference", "原图路径": name + ".png",
                     "来源文件": "anonymous.xlsx", "来源 sheet": "模版", "来源行": row}
                    for name, row in (("brand", 7), ("people", 20))]
        analysis = [
            {"image_id": "brand", "caption": "城市天际线背景下的品牌标志片头", "tags": ["logo", "城市"], "source": "fixture"},
            {"image_id": "people", "caption": "户外入口广场，人群在展板前拍照打卡交流", "tags": ["人群", "外场", "签到", "互动"], "source": "fixture"},
        ]
        result = search_images_for_slot(slot, {"脚本类型": "发布会"}, manifest, analysis)
        self.assertEqual(result["candidates"][0]["image_id"], "people")
        brand = next(item for item in result["candidates"] if item["image_id"] == "brand")
        self.assertEqual(brand["match_tier"], "reference")
        self.assertFalse(brand["default_eligible"])

    def test_similar_fit_within_five_points_prefers_an_unused_composition(self):
        def candidate(key, score, coverage="people,exterior", tier="matched"):
            return {"image_id": key, "score": score, "perceptual_cluster_id": key,
                    "match_tier": tier, "coverage_signature": coverage}
        results = {
            "1": {"candidates": [candidate("a", 90)]},
            "2": {"candidates": [candidate("a", 90), candidate("b", 86)]},
            "3": {"candidates": [candidate("a", 90), candidate("c", 86, "people")]},
            "4": {"candidates": [candidate("a", 90), candidate("d", 84)]},
        }
        allocation = allocate_diverse_defaults(results)
        self.assertEqual(allocation["selected_by_shot"], {"1": "a", "2": "b", "3": "a", "4": "a"})

    def test_full_library_includes_unlinked_images_without_claiming_a_shot_match(self):
        media = [{"media_id": "unlinked", "path": "/anonymous/unlinked.png", "content_sha256": "a" * 64, "caption": "户外入口与人群", "tags": ["人群"],
                  "source_links": [], "visual_analysis": {"caption": "户外入口与人群", "tags": ["人群"], "source": "fixture"}}]
        result = search_package_library_candidates(brief={"脚本类型": "发布会"}, media=media, top_k=None, browse=True)
        self.assertEqual([item["image_id"] for item in result["candidates"]], ["unlinked"])
        self.assertEqual(result["candidates"][0]["match_tier"], "unassigned")

    def test_explicit_aerial_field_cannot_be_erased_by_generic_visual_text(self):
        slot = formal_image_search_slot(row={"镜号": "1", "画面描述": "现代建筑外场与入口", "类型": "航拍"},
                                        detail={}, planned={}, fallback_index=1)
        assets = [{"image_id": "ground", "asset_role": "source_reference", "原图路径": "/anonymous/ground.png"},
                  {"image_id": "aerial", "asset_role": "source_reference", "原图路径": "/anonymous/aerial.png"}]
        analysis = [{"image_id": "ground", "caption": "地面视角的现代建筑外场与入口", "tags": ["建筑", "外场", "地面"], "brand_names": ["无"]},
                    {"image_id": "aerial", "caption": "航拍俯视现代建筑外场与入口", "tags": ["建筑", "航拍", "俯视"], "brand_names": ["无"]}]
        from mvp_v2.image_retrieval import search_images_for_slot
        candidates = search_images_for_slot(slot, {"脚本类型": "发布会"}, assets, analysis, top_k=8)["candidates"]
        by_id = {item["image_id"]: item for item in candidates}
        self.assertTrue(by_id["aerial"]["default_eligible"])
        self.assertFalse(by_id["ground"]["default_eligible"])
        self.assertEqual(candidates[0]["image_id"], "aerial")

    def test_precise_subject_action_and_angle_evidence_precedes_source_bonus(self):
        examples = [
            ("音乐节", "低机位仰拍歌手与圆形屏", "观众举手机拍摄舞台", "低机位仰拍歌手演唱，身后圆形屏清晰"),
            ("明星活动", "艺人持麦致辞", "建筑前张开双臂的人物剪影", "艺人手持麦克风向观众致辞"),
            ("赛事", "面部汗水与喘息特写，随后握手", "低角度运动鞋和腿部构图", "运动员面部汗水与喘息特写，正在握手"),
        ]
        for category, query, wrong, right in examples:
            with self.subTest(category=category):
                slot = formal_image_search_slot(row={"镜号": "1", "画面描述": query}, detail={}, planned={}, fallback_index=1)
                manifest = [{"image_id": key, "asset_role": "source_reference"} for key in ("wrong", "right")]
                analysis = [{"image_id": key, "caption": caption, "tags": [category], "brand_names": ["无"]}
                            for key, caption in (("wrong", wrong), ("right", right))]
                result = search_images_for_slot(slot, {"脚本类型": category}, manifest, analysis, top_k=8)
                by_id = {item["image_id"]: item for item in result["candidates"]}
                self.assertEqual(result["candidates"][0]["image_id"], "right")
                self.assertTrue(by_id["right"]["default_eligible"])
                self.assertFalse(by_id["wrong"]["default_eligible"])
                self.assertTrue(by_id["wrong"]["missing"])

    def test_structure_stage_is_not_an_extra_subject_requirement(self):
        slot = formal_image_search_slot(row={"镜号": "1", "画面描述": "航拍会展中心外立面", "结构": "人气与现场互动"},
                                        detail={}, planned={}, fallback_index=1)
        result = search_images_for_slot(slot, {"脚本类型": "发布会"}, [{"image_id": "building", "asset_role": "source_reference"}],
            [{"image_id": "building", "caption": "航拍会展中心建筑外立面", "tags": ["航拍", "建筑"], "brand_names": ["无"]}], top_k=8)
        self.assertNotIn("互动体验", result["candidates"][0]["missing"])
        self.assertTrue(result["candidates"][0]["default_eligible"])
