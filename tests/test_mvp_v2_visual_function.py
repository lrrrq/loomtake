import unittest

from mvp_v2.visual_function import (
    annotate_visual_function_fields,
    audit_visual_function_quality,
    infer_visual_function,
)


class VisualFunctionTests(unittest.TestCase):
    def test_audience_recording_wins_over_service_detail(self):
        self.assertEqual(
            infer_visual_function("观众围着爆米花机，纷纷举起手机拍摄记录"),
            "audience_recording",
        )

    def test_venue_exterior_is_a_distinct_primary_function(self):
        self.assertEqual(
            infer_visual_function("发布会外场入口与城市天际线，品牌logo位于门头中央"),
            "exterior_landmark",
        )

    def test_brand_slogan_typography_is_brand_visual(self):
        self.assertEqual(
            infer_visual_function("黑色背景上以白色艺术字体展示品牌标语"),
            "brand_visual",
        )

    def test_guests_standing_and_applauding_on_stage_are_a_formal_guest_node(self):
        self.assertEqual(
            infer_visual_function("七位嘉宾在发布会舞台并排站立鼓掌，现场氛围庄重"),
            "formal_guest_node",
        )

    def test_guest_photo_and_formal_stage_node_are_not_duplicate_functions(self):
        checks = audit_visual_function_quality([
            {
                "slot_index": 6,
                "模板镜号": "6",
                "image_id": "guest-photo",
                "视觉内容摘要": "四位嘉宾在品牌背景板前合影留念",
            },
            {
                "slot_index": 15,
                "模板镜号": "15",
                "image_id": "formal-stage-node",
                "视觉内容摘要": "四位嘉宾站在发布会舞台中央完成品牌亮相并鼓掌",
            },
        ])
        self.assertNotIn(
            "duplicate_visual_function",
            {check["检查项"] for check in checks},
        )

    def test_startup_action_cannot_replace_guest_backing_slot(self):
        checks = audit_visual_function_quality([{
            "slot_index": 6,
            "模板镜号": "6",
            "image_id": "startup",
            "视觉内容摘要": "多位嘉宾共同触碰发光球完成启动仪式",
            "视觉标签": "嘉宾合影；启动仪式",
        }])
        self.assertIn(
            "visual_function_mismatch",
            {check["检查项"] for check in checks},
        )

        checks = audit_visual_function_quality([{
            "slot_index": 15,
            "模板镜号": "15",
            "image_id": "formal-guest-node",
            "视觉内容摘要": "七位嘉宾在发布会舞台并排站立鼓掌，现场氛围庄重",
        }])
        self.assertNotIn(
            "visual_function_mismatch",
            {check["检查项"] for check in checks},
        )

    def test_different_images_with_same_wrong_function_are_reported(self):
        checks = audit_visual_function_quality([
            {
                "slot_index": 12,
                "模板镜号": "12",
                "image_id": "img_speech_a",
                "视觉内容摘要": "男性演讲者站在舞台中央讲解战略数据",
            },
            {
                "slot_index": 15,
                "模板镜号": "15",
                "image_id": "img_speech_b",
                "视觉内容摘要": "女性演讲者站在LED大屏前发表演讲",
            },
        ])

        items = {check["检查项"] for check in checks}
        self.assertIn("visual_function_mismatch", items)
        self.assertIn("duplicate_visual_function", items)

    def test_documented_slot_adaptation_is_not_a_function_mismatch(self):
        checks = audit_visual_function_quality([{
            "slot_index": 2,
            "模板镜号": "2",
            "image_id": "img_exterior",
            "视觉内容摘要": "发布会外场入口，品牌logo与主视觉建立活动场地",
        }])

        self.assertNotIn(
            "visual_function_mismatch",
            {check["检查项"] for check in checks},
        )

    def test_expected_venue_callbacks_do_not_create_function_duplicate_warning(self):
        checks = audit_visual_function_quality([
            {
                "slot_index": slot,
                "模板镜号": str(slot),
                "image_id": f"img_venue_{slot}",
                "视觉内容摘要": "发布会主会场舞台全景，大型LED屏与观众席完整入画",
            }
            for slot in (4, 11, 17)
        ])

        self.assertNotIn(
            "duplicate_visual_function",
            {check["检查项"] for check in checks},
        )

    def test_multifunction_image_uses_slot_expected_function_for_duplicate_audit(self):
        checks = audit_visual_function_quality([
            {
                "slot_index": 11, "模板镜号": "11", "image_id": "venue-speech",
                "视觉内容摘要": "发布会主会场舞台全景，演讲者站在大屏前发言",
            },
            {
                "slot_index": 12, "模板镜号": "12", "image_id": "speech-close",
                "视觉内容摘要": "演讲者在舞台中央发表演讲",
            },
        ])

        self.assertNotIn(
            "duplicate_visual_function",
            {check["检查项"] for check in checks},
        )

    def test_adjacent_startup_actions_are_not_accepted_as_distinct_ceremony_steps(self):
        checks = audit_visual_function_quality([
            {
                "slot_index": 15, "模板镜号": "15", "image_id": "ceremony-stage",
                "视觉内容摘要": "多位嘉宾共同登台完成启动仪式",
            },
            {
                "slot_index": 16, "模板镜号": "16", "image_id": "startup-close",
                "视觉内容摘要": "嘉宾共同触碰发光球启动装置，形成高潮",
            },
        ])

        items = {check["检查项"] for check in checks}
        self.assertIn("visual_function_mismatch", items)
        self.assertIn("duplicate_visual_function", items)

    def test_interaction_to_experience_progression_is_not_duplicate_warning(self):
        checks = audit_visual_function_quality([
            {
                "slot_index": 5, "模板镜号": "5", "image_id": "device-demo",
                "视觉内容摘要": "机器人操作智能设备进行产品体验演示",
            },
            {
                "slot_index": 7, "模板镜号": "7", "image_id": "guest-experience",
                "视觉内容摘要": "参会者与机器人互动并体验设备",
            },
        ])

        self.assertNotIn(
            "duplicate_visual_function",
            {check["检查项"] for check in checks},
        )

    def test_guest_context_to_crowd_heat_progression_is_not_duplicate_warning(self):
        checks = audit_visual_function_quality([
            {
                "slot_index": 6, "模板镜号": "6", "image_id": "event-guests",
                "视觉内容摘要": "发布会现场观众聚集参观交流",
            },
            {
                "slot_index": 9, "模板镜号": "9", "image_id": "crowd-heat",
                "视觉内容摘要": "展区通道人群密集，参观者持续流动",
            },
        ])

        self.assertNotIn(
            "duplicate_visual_function",
            {check["检查项"] for check in checks},
        )

    def test_speech_transition_prefers_speech_over_phone_recording(self):
        details = [{"slot_index": 14, "模板镜号": "14", "image_id": "speech-transition"}]
        assets = [{
            "slot_index": 14,
            "镜号": "14",
            "image_id": "speech-transition",
            "视觉内容摘要": "演讲者站在舞台发言，前景观众用手机拍摄记录",
        }]

        annotate_visual_function_fields(details, assets)

        self.assertEqual(assets[0]["主视觉功能"], "演讲宣讲")

    def test_core_speech_to_speech_transition_is_not_duplicate_warning(self):
        checks = audit_visual_function_quality([
            {
                "slot_index": 12, "模板镜号": "12", "image_id": "core-speech",
                "视觉内容摘要": "演讲者站在舞台中央讲解核心数据",
            },
            {
                "slot_index": 14, "模板镜号": "14", "image_id": "speech-transition",
                "视觉内容摘要": "演讲者站在舞台发言，前景观众用手机拍摄记录",
            },
        ])

        self.assertNotIn(
            "duplicate_visual_function",
            {check["检查项"] for check in checks},
        )

    def test_unclassified_bound_visual_is_reported(self):
        checks = audit_visual_function_quality([{
            "slot_index": 7,
            "模板镜号": "7",
            "image_id": "img_unknown",
            "视觉内容摘要": "抽象色块与模糊光斑",
        }])

        self.assertIn(
            "visual_function_unclassified",
            {check["检查项"] for check in checks},
        )

    def test_multifunction_crowd_image_can_satisfy_crowd_slot(self):
        checks = audit_visual_function_quality([{
            "slot_index": 9,
            "模板镜号": "9",
            "image_id": "img_crowd_phone",
            "视觉内容摘要": "发布会展厅内观众聚集参观，多人使用手机拍摄产品",
        }])

        self.assertNotIn(
            "visual_function_mismatch",
            {check["检查项"] for check in checks},
        )

    def test_annotation_writes_function_and_duplicate_group_to_review_rows(self):
        details = [
            {"slot_index": 12, "模板镜号": "12", "image_id": "speech_a"},
            {"slot_index": 15, "模板镜号": "15", "image_id": "speech_b"},
        ]
        assets = [
            {"slot_index": 12, "镜号": "12", "image_id": "speech_a", "视觉内容摘要": "演讲者在舞台发言"},
            {"slot_index": 15, "镜号": "15", "image_id": "speech_b", "视觉内容摘要": "演讲嘉宾站在大屏前发表演讲"},
        ]

        annotate_visual_function_fields(details, assets)

        self.assertEqual(assets[0]["主视觉功能"], "演讲宣讲")
        self.assertEqual(assets[1]["功能适配判断"], "不匹配")
        self.assertIn("演讲宣讲", details[0]["功能重复组"])
        self.assertEqual(details[0]["功能重复组"], details[1]["功能重复组"])

    def test_annotation_does_not_leave_function_label_blank_when_evidence_is_missing(self):
        details = [{"slot_index": 7, "模板镜号": "7", "image_id": "unknown"}]
        assets = [{"slot_index": 7, "镜号": "7", "image_id": "unknown"}]

        annotate_visual_function_fields(details, assets)

        self.assertEqual(assets[0]["视觉功能标签"], "未分类（视觉证据不足）")
        self.assertEqual(assets[0]["功能适配判断"], "未分类")


if __name__ == "__main__":
    unittest.main()
