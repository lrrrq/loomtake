import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from PIL import Image

from mvp_v2.launch_entry import main as launch_main
from mvp_v2.preflight import inspect_launch_preflight, run_launch_preflight
from mvp_v2.preflight import CheckResult, PreflightReport


def _write_brief(path: Path, material_dir: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append([
        "项目名称", "项目区域文件名", "脚本类型", "品牌或客户", "行业或活动类别",
        "视频时长", "风格调性", "必须出现的内容", "禁忌或不能出现的内容",
        "素材目录或素材文件", "备注",
    ])
    ws.append([
        "测试发布会", "测试发布会.xlsx", "发布会", "测试品牌", "科技发布会",
        "60s", "科技感", "品牌logo", "竞品",
        str(material_dir), "用于入口测试",
    ])
    wb.save(path)


def _write_source(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "模版"
    ws.cell(5, 1, "结构")
    ws.cell(5, 2, "镜号")
    ws.cell(5, 3, "内容")
    for col, value in enumerate(["画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注"], 3):
        ws.cell(6, col, value)
    for row in range(7, 25):
        ws.cell(row, 1, "开场造势")
        ws.cell(row, 2, row - 6)
        ws.cell(row, 9, f"测试镜头{row - 6}")
    creative = wb.create_sheet("创意库")
    creative.cell(5, 1, "镜号")
    creative.cell(5, 2, "结构")
    creative.cell(5, 3, "形式")
    creative.cell(5, 4, "内容")
    for col, value in enumerate(["画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"], 4):
        creative.cell(6, col, value)
    creative.cell(7, 1, "C001")
    creative.cell(7, 2, "开场造势")
    creative.cell(7, 9, "测试创意")
    ai_spec = wb.create_sheet("AI_规范表")
    ai_spec.cell(1, 1, "测试 fixture")
    wb.save(path)


class LaunchEntryTests(unittest.TestCase):
    def test_read_only_preflight_does_not_create_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "not-created" / "out"

            report = inspect_launch_preflight(
                brief_data={
                    "项目名称": "匿名测试",
                    "项目区域文件名": "匿名测试.xlsx",
                    "脚本类型": "发布会",
                    "品牌或客户": "匿名品牌",
                    "行业或活动类别": "发布会",
                    "视频时长": "60s",
                    "风格调性": "克制",
                    "必须出现的内容": "品牌标识",
                    "禁忌或不能出现的内容": "竞品",
                    "素材目录或素材文件": "待补",
                    "备注": "匿名 fixture",
                },
                source_root=root / "missing-source",
                output_dir=output,
                asset_cache_root=root / "cache",
            )

            self.assertFalse(output.exists())
            self.assertTrue(any(
                check.name == "输出目录" and check.status == "pass"
                for check in report.checks
            ))

    def test_preflight_passes_valid_launch_inputs_without_provider_when_cache_is_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            source_root.mkdir()
            _write_source(source_root / "M+发布会类通用脚本库.xlsx")
            media = root / "media"
            media.mkdir()
            Image.new("RGB", (8, 8), "blue").save(media / "01.png")
            brief = root / "brief.xlsx"
            _write_brief(brief, media)
            cache = root / "cache"
            cache_dir = cache / "media"
            cache_dir.mkdir(parents=True)
            (cache_dir / "image_analysis.minimax.jsonl").write_text('{"image_id":"x"}\n', encoding="utf-8")

            with patch("mvp_v2.preflight.minimax_credentials_available", return_value=False):
                report = run_launch_preflight(
                    brief_path=brief,
                    source_root=source_root,
                    output_dir=root / "out",
                    asset_cache_root=cache,
                    allow_cached_analysis=True,
                )

            self.assertTrue(report.passed)
            self.assertTrue(any(check.status == "warn" for check in report.checks))
            self.assertTrue(any(
                check.name == "视觉AI预计调用" and "新增 0 次" in check.message
                for check in report.checks
            ))

    def test_preflight_rejects_missing_source_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / "media"
            media.mkdir()
            Image.new("RGB", (8, 8), "blue").save(media / "01.png")
            brief = root / "brief.xlsx"
            _write_brief(brief, media)

            report = run_launch_preflight(
                brief_path=brief,
                source_root=root / "missing-source",
                output_dir=root / "out",
                asset_cache_root=root / "cache",
            )

            self.assertFalse(report.passed)
            self.assertTrue(any(check.name in {"资料库目录", "发布会脚本库"} for check in report.checks if check.status == "fail"))

    def test_entry_stops_before_pipeline_when_preflight_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief = root / "brief.txt"
            brief.write_text("项目名称：测试\n脚本类型：发布会\n视频时长：59s\n", encoding="utf-8")
            with patch("mvp_v2.launch_entry.pipeline_main") as pipeline:
                code = launch_main([
                    "--brief-file", str(brief),
                    "--source-root", str(root / "missing-source"),
                    "--output-dir", str(root / "out"),
                ])
            self.assertEqual(code, 2)
            pipeline.assert_not_called()

    def test_entry_passes_resume_and_retry_stage_to_auto_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief = root / "brief.txt"
            brief.write_text(
                "项目名称：测试发布会\n脚本类型：发布会\n视频时长：60s\n"
                "风格调性：科技感\n必须出现的内容：品牌logo\n",
                encoding="utf-8",
            )
            preflight = PreflightReport(
                brief={"项目名称": "测试发布会"},
                project_dir=root,
                output_dir=root / "out",
                checks=(CheckResult("测试", "pass", "ok"),),
            )
            with patch(
                "mvp_v2.launch_entry.run_launch_preflight",
                return_value=preflight,
            ), patch(
                "mvp_v2.launch_entry.pipeline_main",
                return_value=3,
            ) as pipeline:
                code = launch_main([
                    "--brief-file", str(brief),
                    "--output-dir", str(root / "out"),
                    "--resume", "run-123",
                    "--retry-stage", "human_review",
                ])

            self.assertEqual(code, 3)
            args = pipeline.call_args.args[0]
            self.assertEqual(args[args.index("--resume") + 1], "run-123")
            self.assertEqual(
                args[args.index("--retry-stage") + 1],
                "human_review",
            )
