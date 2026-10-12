import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

from mvp_v2.client_quality import audit_client_visible_content
from mvp_v2.client_workbook import (
    ClientWorkbookError,
    validate_client_review_workbook,
    write_client_review_workbook,
)
from mvp_v2.revision import extract_revision_constraints, write_revision_constraints


class ClientDeliveryTests(unittest.TestCase):
    def test_client_quality_blocks_old_brand_internal_language_and_placeholder(self):
        checks = audit_client_visible_content(
            {"项目名称": "周大福发布会", "品牌或客户": "周大福"},
            [{
                "镜号": "1",
                "结构": "开场",
                "画面描述": "林氏家居画面，呈现Brief要求的家电/科技发布会核心内容",
                "声音": "音乐",
            }],
            [{"来源品牌": "林氏家居"}],
            overall_summary="周大福项目",
            quality_profile="client-review",
        )

        hard_items = {
            item["检查项"] for item in checks if item["级别"] == "hard"
        }
        self.assertIn("client_brand_leak", hard_items)
        self.assertIn("client_internal_process_language", hard_items)
        self.assertIn("client_fallback_placeholder", hard_items)

    def test_client_quality_blocks_ai_production_language(self):
        checks = audit_client_visible_content(
            {"项目名称": "城市音乐节", "品牌或客户": "折光原野"},
            [{"镜号": "1", "画面描述": "AI合成折光原野主舞台画面"}],
            [{}],
            quality_profile="client-review",
        )

        self.assertIn(
            "client_internal_process_language",
            {item["检查项"] for item in checks if item["级别"] == "hard"},
        )

    def test_client_quality_blocks_generated_required_content_marker(self):
        checks = audit_client_visible_content(
            {"项目名称": "测试发布会", "品牌或客户": "测试品牌"},
            [{"镜号": "7", "画面描述": "镜头明确呈现主题装置"}],
            [{}],
            quality_profile="client-review",
        )

        self.assertIn(
            "client_internal_process_language",
            {item["检查项"] for item in checks if item["级别"] == "hard"},
        )

    def test_pending_confirmation_is_warning_not_hard_fail(self):
        checks = audit_client_visible_content(
            {"项目名称": "测试发布会", "品牌或客户": "测试品牌"},
            [{"镜号": "1", "结构": "开场", "画面描述": "产品系列待确认"}],
            [{}],
            overall_summary="测试项目说明",
            pending_confirmations=[{"shot_no": "1", "item": "产品系列"}],
            quality_profile="client-review",
        )

        self.assertFalse(any(item["级别"] == "hard" for item in checks))
        self.assertEqual(checks[-1]["检查项"], "client_pending_confirmation")

    def test_excessive_pending_confirmations_are_hard_fail(self):
        checks = audit_client_visible_content(
            {"项目名称": "测试发布会", "品牌或客户": "测试"},
            [{"镜号": "1", "结构": "开场", "画面描述": "测试品牌主视觉入画"}],
            [{}],
            pending_confirmations=[
                {"shot_no": str(index), "item": f"事项{index}"}
                for index in range(1, 8)
            ],
            quality_profile="client-review",
        )

        self.assertIn(
            "client_pending_confirmation_overuse",
            {item["检查项"] for item in checks if item["级别"] == "hard"},
        )

    def test_unsupported_numeric_fact_is_hard_fail(self):
        checks = audit_client_visible_content(
            {"项目名称": "美的发布会", "品牌或客户": "美的"},
            [{"镜号": "1", "结构": "落版", "画面描述": "智美万象2026主视觉落版"}],
            [{"采用来源证据": "智美万象主视觉落版"}],
            quality_profile="client-review",
        )

        self.assertIn(
            "client_unsupported_numeric_fact",
            {item["检查项"] for item in checks if item["级别"] == "hard"},
        )

    def test_360_panorama_is_camera_language_but_unsupported_guest_count_still_fails(self):
        row = {'镜号': '1', '景别': '360全景', '画面描述': '展示会场空间', '声音': '环境声'}
        checks = audit_client_visible_content({}, [row], [], quality_profile='client-review')
        self.assertNotIn('client_unsupported_numeric_fact', {item['检查项'] for item in checks})
        row['画面描述'] = '360位嘉宾入场'
        checks = audit_client_visible_content({}, [row], [], quality_profile='client-review')
        self.assertIn('client_unsupported_numeric_fact', {item['检查项'] for item in checks})

    def test_duration_unit_variants_are_the_same_supported_fact(self):
        checks = audit_client_visible_content(
            {"项目名称": "测试展会", "品牌或客户": "测试品牌", "视频时长": "90s"},
            [{"镜号": "1", "画面描述": "90秒展会开场"}],
            [{}],
            quality_profile="client-review",
        )

        self.assertNotIn("client_unsupported_numeric_fact", {item["检查项"] for item in checks})

    def test_client_quality_blocks_unknown_source_brand_without_static_alias(self):
        checks = audit_client_visible_content(
            {"项目名称": "星河科技发布会", "品牌或客户": "星河科技"},
            [{"镜号": "1", "画面描述": "另一科技发布会主会场全景"}],
            [{"采用来源品牌": "另一科技"}],
            quality_profile="client-review",
        )

        self.assertIn(
            "client_brand_leak",
            {item["检查项"] for item in checks if item["级别"] == "hard"},
        )

    def test_client_quality_accepts_current_brand_case_insensitively(self):
        checks = audit_client_visible_content(
            {"项目名称": "vivo发布会", "品牌或客户": "vivo"},
            [{"镜号": "1", "画面描述": "VIVO品牌logo进入主会场"}],
            [{}],
            quality_profile="client-review",
        )

        self.assertNotIn("client_brand_leak", {item["检查项"] for item in checks})

    def test_client_workbook_keeps_only_script_and_pending_sheets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            internal = root / "internal.xlsx"
            workbook = Workbook()
            workbook.active.title = "脚本"
            workbook["脚本"]["A1"] = "客户脚本"
            workbook.create_sheet("抽调明细")["A1"] = "来源文件"
            workbook.create_sheet("质量审计")
            workbook.save(internal)
            workbook.close()

            output = write_client_review_workbook(
                internal,
                root / "client.xlsx",
                [{"shot_no": "4", "item": "装置名称"}],
            )
            result = validate_client_review_workbook(output)

            self.assertEqual(result["sheet_names"], ["脚本", "待确认事项"])
            self.assertEqual(result["pending_count"], 1)
            reopened = load_workbook(output, read_only=True, data_only=True)
            self.assertEqual(reopened["待确认事项"]["B2"].value, "装置名称")
            reopened.close()

    def test_client_workbook_strips_internal_brief_annotations_from_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            internal = root / "internal.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "脚本"
            sheet.append(["镜号", "画面描述", "备注"])
            sheet.append([
                "1",
                "当前品牌 Logo 入画",
                "建议时长：4s；Brief必选：当前品牌 Logo；拍摄待确认",
            ])
            workbook.save(internal)
            workbook.close()

            output = write_client_review_workbook(internal, root / "client.xlsx")

            reopened = load_workbook(output, read_only=True, data_only=True)
            note = reopened["脚本"]["C2"].value
            reopened.close()
            self.assertEqual(note, "建议时长：4s；拍摄待确认")

    def test_client_workbook_rejects_residual_internal_process_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.xlsx"
            workbook = Workbook()
            workbook.active.title = "脚本"
            workbook["脚本"]["A1"] = "这是AI合成的脚本"
            workbook.create_sheet("待确认事项")
            workbook.save(path)
            workbook.close()

            with self.assertRaisesRegex(ClientWorkbookError, "内部过程语言"):
                validate_client_review_workbook(path)

    def test_client_workbook_rejects_absolute_paths_and_formulas(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.xlsx"
            workbook = Workbook()
            workbook.active.title = "脚本"
            workbook["脚本"]["A1"] = "/Users/example/private-fixture.txt"
            workbook.create_sheet("待确认事项")["A1"] = "=1+1"
            workbook.save(path)
            workbook.close()

            with self.assertRaises(ClientWorkbookError):
                validate_client_review_workbook(path)

    def test_revision_constraints_only_extract_rows_requiring_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.xlsx"
            workbook = Workbook()
            workbook.active.title = "剪辑复核"
            workbook["剪辑复核"].append([
                "镜号", "结构审核", "总体审核", "修改意见", "审核人", "审核日期",
            ])
            workbook["剪辑复核"].append(["1", "通过", "通过", "", "审核人", "2026-08-04"])
            workbook["剪辑复核"].append(["2", "需修改", "需修改", "收紧画面主体", "审核人", "2026-08-04"])
            workbook.create_sheet("相邻镜头复核").append(["前镜号", "后镜号", "总体审核", "修改意见"])
            workbook.create_sheet("整片复核").append(["总体审核", "修改意见"])
            workbook.save(path)
            workbook.close()

            constraints = extract_revision_constraints(path)

            self.assertEqual(len(constraints), 1)
            self.assertEqual(constraints[0]["shot_no"], "2")
            self.assertEqual(constraints[0]["comment"], "收紧画面主体")

    def test_revision_comments_are_issues_even_when_status_is_blank_or_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "剪辑复核"
            sheet.append([
                "镜号", "结构", "画面描述", "声音", "总体审核", "修改意见",
            ])
            sheet.append(["1", "开场", "原画面", "音乐", "", "开头应该是空镜展示"])
            sheet.append(["2", "发展", "原画面", "音乐", "通过", "人物到场与下方重复"])
            sheet.append(["3", "收束", "原画面", "音乐", "通过", ""])
            workbook.create_sheet("相邻镜头复核").append(["前镜号", "后镜号", "总体审核", "修改意见"])
            workbook.create_sheet("整片复核").append(["总体审核", "修改意见"])
            workbook.save(path)
            workbook.close()

            constraints = extract_revision_constraints(path)
            output = write_revision_constraints(
                path, Path(tmp) / "constraints.json", parent_run_id="run_1",
            )
            payload = __import__("json").loads(output.read_text(encoding="utf-8"))

            self.assertEqual([item["shot_no"] for item in constraints], ["1", "2"])
            self.assertEqual([item["category"] for item in constraints], ["structure", "structure"])
            self.assertEqual(constraints[0]["statuses"], ["复核提出"])
            self.assertEqual(constraints[1]["declared_statuses"], ["通过"])
            self.assertEqual(
                payload["confirmed_review_requirements"][0]["classification"],
                "position_constraint",
            )
            self.assertEqual(
                payload["confirmed_review_requirements"][0]["decision_source"],
                "template_contract_review",
            )

    def test_reviewed_cell_changes_and_added_flow_become_consumable_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "review.xlsx"
            workbook = Workbook()
            script = workbook.active
            script.title = "脚本"
            script.append(["结构", "镜号", "内容", "声音"])
            script.append(["开场", "1", "原画面", "音乐"])
            review = workbook.create_sheet("剪辑复核")
            review.append(["镜号", "结构", "画面描述", "声音", "总体审核", "修改意见"])
            review.append(["1", "空镜建立", "外场空镜", "环境声", "", "文字结构不对（已改正）"])
            review.append(["", "颁奖仪式", "各地区代表颁奖仪式", "", "", "少了一个颁奖仪式"])
            workbook.create_sheet("相邻镜头复核").append(["前镜号", "后镜号", "总体审核", "修改意见"])
            whole = workbook.create_sheet("整片复核")
            whole.append(["总体审核", "修改意见"])
            whole.append(["", "少了国外代表区域人物发言的内容"])
            workbook.save(path)
            workbook.close()

            constraints = extract_revision_constraints(path)
            output = write_revision_constraints(path, root / "constraints.json", parent_run_id="run_1")
            payload = __import__("json").loads(output.read_text(encoding="utf-8"))

            self.assertEqual(len(constraints), 3)
            self.assertIn("结构=空镜建立", constraints[0]["desired_change"])
            self.assertIn("画面描述=外场空镜", constraints[0]["desired_change"])
            self.assertEqual(constraints[1]["scope"], "whole_film")
            self.assertEqual(payload["restart_stage"], "script_plan")
            self.assertEqual(len(payload["user_feedback"]), 3)
            self.assertEqual(payload["user_feedback"][0]["scope"], "shot")
            self.assertEqual(payload["user_feedback"][1]["category"], "structure")
            self.assertEqual(len(payload["confirmed_review_requirements"]), 3)
            self.assertTrue(all(
                item["evidence_refs"][0]["source"]["sha256"]
                == payload["parent_review_sha256"]
                for item in payload["confirmed_review_requirements"]
            ))

    def test_wrong_review_image_is_excluded_from_the_next_visual_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "review.xlsx"
            workbook = Workbook()
            review = workbook.active
            review.title = "剪辑复核"
            review.append([
                "镜号", "image_id", "原图路径", "总体审核", "修改意见",
            ])
            review.append(["8", "img_wrong", "/anonymous/wrong.png", "", "配图不对"])
            workbook.create_sheet("相邻镜头复核").append(["前镜号", "后镜号", "总体审核", "修改意见"])
            workbook.create_sheet("整片复核").append(["总体审核", "修改意见"])
            workbook.save(path)
            workbook.close()

            output = write_revision_constraints(
                path, root / "constraints.json", parent_run_id="run_1"
            )
            payload = __import__("json").loads(output.read_text(encoding="utf-8"))

            self.assertEqual(payload["restart_stage"], "visual_binding")
            self.assertEqual(
                payload["exclude_visuals_by_slot"]["8"],
                ["img_wrong", "/anonymous/wrong.png"],
            )


if __name__ == "__main__":
    unittest.main()
