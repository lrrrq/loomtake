import copy
import unittest

from mvp_v2.client_quality import audit_client_visible_content


class ContentConsistencyTests(unittest.TestCase):
    def _audit(self, rows, *, profile="draft", details=None):
        return audit_client_visible_content({"品牌或客户": "测试品牌"}, rows, details or [], quality_profile=profile)

    def test_explicit_field_content_conflicts_are_explained_warnings_in_all_profiles(self):
        cases = [
            ({"类型": "航拍", "画面描述": "地面机位手持跟随嘉宾走入会场。"}, "content_camera_conflict", "航拍"),
            ({"类型": "地面", "画面描述": "无人机航拍俯瞰场馆外观。"}, "content_camera_conflict", "地面"),
            ({"结构": "会场空镜", "画面描述": "主持人走上舞台，向观众挥手。"}, "content_empty_scene_people_conflict", "主持人"),
            ({"景别": "特写", "画面描述": "全景展示舞台与观众席。"}, "content_shot_size_conflict", "全景"),
            ({"结构": "舞台表演", "画面描述": "演讲者站在讲台发表演讲。"}, "content_structure_conflict", "演讲"),
        ]
        for profile in ("draft", "client-review", "anonymous-profile"):
            for value, item, evidence in cases:
                with self.subTest(profile=profile, item=item, value=value):
                    row = {"镜号": "7", "时长(秒)": 4, **value}
                    before = copy.deepcopy(row)
                    checks = self._audit([row], profile=profile)
                    selected = [check for check in checks if check["检查项"] == item]
                    self.assertEqual(len(selected), 1)
                    self.assertEqual(selected[0]["级别"], "warning")
                    self.assertEqual(selected[0]["状态"], "warning")
                    self.assertEqual(selected[0]["关联镜号"], "7")
                    self.assertIn(evidence, selected[0]["详情"])
                    self.assertEqual(row, before)

    def test_short_duration_with_separate_events_has_measured_review_evidence(self):
        row = {"镜号": "9", "时长(秒)": 2,
            "画面描述": "来宾签到登记，嘉宾发表演讲，工作人员颁奖授牌。"}
        check = next(item for item in self._audit([row]) if item["检查项"] == "content_duration_overload")
        self.assertEqual(check["级别"], "warning")
        self.assertIn("2", check["详情"])
        self.assertIn("3", check["详情"])
        self.assertIn("签到", check["详情"])

    def test_dense_spoken_copy_is_warning_and_empty_copy_is_allowed(self):
        row = {"镜号": "A", "时长(秒)": 2, "画面描述": "当前品牌标识缓缓亮起。",
            "旁白": "欢迎来到本次活动现场一起体验全新产品感受未来生活的无限可能", "字幕": ""}
        checks = self._audit([row])
        self.assertTrue(any(item["检查项"] == "content_copy_duration_overload" for item in checks))
        row["旁白"] = ""
        self.assertFalse(any("duration_overload" in item["检查项"] for item in self._audit([row])))

    def test_neighbor_order_warning_identifies_exact_edge_without_fixed_shot_count(self):
        rows = [{"镜号": "3", "结构": "片尾落版", "画面描述": "当前品牌主视觉落版。"},
            {"镜号": "8", "结构": "开场建立", "画面描述": "活动尚未开始，嘉宾陆续入场。"}]
        selected = [item for item in self._audit(rows) if item["检查项"] == "content_transition_conflict"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["关联镜号"], "3→8")
        self.assertEqual(selected[0]["级别"], "warning")
        rows[1]["备注"] = "倒叙回顾入场过程"
        self.assertFalse(any(item["检查项"] == "content_transition_conflict" for item in self._audit(rows)))

    def test_explicit_next_shot_instruction_is_compared_to_actual_neighbor(self):
        rows = [{"镜号": "11", "画面描述": "现场观众鼓掌。", "备注": "下一镜切至品牌落版"},
            {"镜号": "12", "画面描述": "演讲者发表演讲。"}]
        selected = [item for item in self._audit(rows) if item["检查项"] == "content_transition_conflict"]
        self.assertEqual(len(selected), 1)
        self.assertIn("品牌落版", selected[0]["详情"])
        self.assertEqual(selected[0]["关联镜号"], "11→12")

    def test_optional_events_negated_actions_and_intentional_scale_changes_are_allowed(self):
        rows = [
            {"镜号": "1", "结构": "开场", "画面描述": "当前品牌标识入画。", "画面示意": "", "旁白": "", "字幕": ""},
            {"镜号": "2", "结构": "舞台表演（可选）", "画面描述": "参观者交流产品体验。"},
            {"镜号": "3", "结构": "会场空镜", "画面描述": "无人会场，未出现观众互动，也不安排主持人发言。"},
            {"镜号": "4", "类型": "地面", "景别": "全景→特写", "画面描述": "非航拍，全景缓慢推近至品牌标识特写。"},
            {"镜号": "5", "画面描述": "当前品牌标识入画。", "备注": "沿用同一品牌视觉呼应开场"},
            {"镜号": "6", "时长(秒)": 2, "画面描述": "蒙太奇快切签到登记、演讲者发言与颁奖授牌。"},
            {"镜号": "7", "结构": "非空镜", "画面描述": "主持人走上舞台。"},
            {"镜号": "8", "类型": "航拍（待确认）", "画面描述": "手持拍摄嘉宾交流。"},
            {"镜号": "9", "结构": "核心节奏·中景承接", "画面描述": "人群沿通道走入会场。"},
        ]
        self.assertEqual(self._audit(rows), [])

    def test_mismatched_detail_positions_do_not_assign_another_shots_structure(self):
        rows = [{"镜号": "5", "画面描述": "演讲者发表演讲。"}]
        self.assertEqual(self._audit(rows, details=[{"模板镜号": "2", "模板结构": "舞台表演"}]), [])

    def test_existing_client_hard_rules_keep_their_profile_boundary(self):
        row = {"镜号": "1", "画面描述": "林氏家居的AI生成核心内容"}
        self.assertFalse(any(item["级别"] == "hard" for item in self._audit([row])))
        self.assertTrue(any(item["级别"] == "hard" for item in self._audit([row], profile="client-review")))


if __name__ == "__main__":
    unittest.main()
