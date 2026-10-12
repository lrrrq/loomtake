import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

from mvp_v2.audit import audit_output_workbook_format
from mvp_v2.excel_io import write_template_output_workbook
from mvp_v2.generator import (
    generate_template_rows,
    rewrite_template_rows_from_visual_evidence,
)
from mvp_v2.models import SourceShot, TemplateDecision
from mvp_v2.visual_assets import audit_visual_evidence


def jewelry_brief() -> dict:
    return {
        "项目名称": "周大福珠宝发布会",
        "项目区域文件名": "周大福珠宝发布会.xlsx",
        "脚本类型": "发布会",
        "品牌或客户": "周大福",
        "行业或活动类别": "珠宝",
        "视频时长": "60s",
        "风格调性": "高级大气，大国品牌",
        "必须出现的内容": "珠宝产品、发布会走秀、明星演讲",
        "禁忌或不能出现的内容": "低质感口号、脱离实际素材",
        "素材目录或素材文件": "/tmp/美的智美万象发布会",
        "备注": "无周大福品牌素材时只允许功能参考",
    }


def template_decisions() -> list[TemplateDecision]:
    descriptions = (
        "蓝色科技字体穿梭入画",
        "科技展馆施工延时",
        "蓝色卡通手形图案快闪",
        "智能家居发布会舞台",
        "机器人操作爆米花机",
        "科技品牌高管合影",
        "来宾体验汽车座舱",
        "智能设备展示区",
        "家电展厅观众参观",
        "蓝色卡通手形图案回扣",
        "游戏主题产品发布会",
        "科技产品发布会演讲",
        "机器人制作爆米花",
        "科技发布会舞台表演",
        "智能家居产品颁奖",
        "科技品牌启动仪式",
        "智能家居会场全景",
        "科技品牌主视觉落版",
    )
    decisions = []
    for index, description in enumerate(descriptions, start=1):
        slot = SourceShot(
            script_type="发布会",
            source_file="M+发布会类通用脚本库.xlsx",
            source_sheet="模版",
            source_row=index + 6,
            shot_no=str(index),
            structure="发布会结构",
            visual_desc=description,
            reuse_level="可复用",
            slot_index=index,
        )
        decisions.append(TemplateDecision(
            slot=slot,
            variant=None,
            mode="KEEP",
            score=100,
            match_reason="模板骨架",
        ))
    return decisions


def write_template_fixture(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "模版"
    ws.merge_cells("A1:K1")
    ws.merge_cells("A5:A6")
    ws.merge_cells("B5:B6")
    ws.merge_cells("C5:K5")
    ws["A1"] = "M+发布会类通用模版"
    ws["A5"] = "结构"
    ws["B5"] = "镜号"
    ws["C6"] = "画面示意"
    ws["I6"] = "画面描述"
    ws.column_dimensions["C"].width = 80
    ws.column_dimensions["I"].width = 60
    ws.row_dimensions[7].height = 148.5
    wb.save(path)


class BriefFirstGenerationTests(unittest.TestCase):
    def test_jewelry_brief_drives_copy_instead_of_technology_template(self):
        script_rows, detail_rows = generate_template_rows(
            jewelry_brief(), template_decisions()
        )

        output_text = " ".join(row["画面描述"] for row in script_rows)
        self.assertEqual(len(script_rows), 18)
        self.assertEqual(sum(row["时长(秒)"] for row in script_rows), 60)
        self.assertIn("周大福", output_text)
        self.assertIn("珠宝产品", output_text)
        self.assertIn("发布会走秀", output_text)
        self.assertIn("明星演讲", output_text)
        for unrelated in ("机器人", "爆米花", "汽车座舱", "家电", "智能家居"):
            self.assertNotIn(unrelated, output_text)
        self.assertTrue(all("本地规则" in row["候选选择理由"] for row in detail_rows))

    def test_visual_caption_cannot_overwrite_brief_and_cross_project_is_reference_only(self):
        script_rows, detail_rows = generate_template_rows(
            jewelry_brief(), template_decisions()
        )
        before = script_rows[4]["画面描述"]
        visual_assets = [{
            "slot_index": 5,
            "图片角色": "画面示意",
            "配图判断": "匹配",
            "视觉内容摘要": "机器人正在操作爆米花机",
            "原图路径": "/tmp/美的智美万象发布会/机器人.png",
            "image_id": "midea-robot",
            "视觉适配模式": "primary",
            "绑定说明": "视觉功能匹配",
        }]

        rewritten, rewritten_details, rewritten_assets = rewrite_template_rows_from_visual_evidence(
            jewelry_brief(), script_rows, detail_rows, visual_assets
        )

        self.assertEqual(rewritten[4]["画面描述"], before)
        self.assertNotIn("机器人", rewritten[4]["画面描述"])
        self.assertEqual(rewritten_assets[0]["配图判断"], "功能参考")
        self.assertEqual(rewritten_details[4]["配图判断"], "功能参考")
        self.assertIn("不可作为品牌或行业匹配素材", rewritten[4]["备注"])
        self.assertIn(
            "visual_function_reference_only",
            {check["检查项"] for check in audit_visual_evidence(rewritten_details)},
        )

    def test_output_title_uses_real_project_name_and_format_audit_accepts_it(self):
        script_rows, detail_rows = generate_template_rows(
            jewelry_brief(), template_decisions()
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "周大福珠宝发布会-发布会脚本.xlsx"
            template = Path(tmp) / "M+发布会类通用脚本库.xlsx"
            write_template_fixture(template)
            write_template_output_workbook(
                output,
                script_rows,
                detail_rows,
                [],
                template_workbook_path=template,
            )
            wb = load_workbook(output, read_only=True, data_only=True)
            try:
                self.assertEqual(
                    wb["脚本"]["A1"].value,
                    "周大福珠宝发布会｜60秒发布会脚本",
                )
            finally:
                wb.close()
            checks = audit_output_workbook_format(output)
            self.assertNotIn(
                "template_layout_missing",
                {check["检查项"] for check in checks if check["级别"] == "hard"},
            )


if __name__ == "__main__":
    unittest.main()
