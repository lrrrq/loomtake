import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as SpreadsheetImage
from PIL import Image

from mvp.excel_io import (
    OUTPUT_COLUMNS,
    BriefError,
    load_source_shots,
    read_briefs,
    write_output_workbook,
)
from mvp.generator import CodexGenerator, GenerationError, build_prompt
from mvp.cli import _default_codex_bin, _default_source_root
from mvp.mapping import detect_scene_type, select_candidates


ROOT = Path(__file__).resolve().parents[1]


def write_wb(path, rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)


def write_source_fixture(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    common = [1, "开场", "A", None, None, "硬切", "全景", "推进", "品牌 logo 特写", "音乐起", ""]
    for scene_type, filename in (
        ("年会", "M+年会类通用脚本库.xlsx"),
        ("展会", "M+展会类通用脚本库.xlsx"),
        ("赛事", "M+赛事类通用脚本库.xlsx"),
        ("音乐节", "M+音乐节通用脚本库.xlsx"),
    ):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = scene_type
        sheet.append([f"M+{scene_type}通用脚本库"])
        sheet.append(["整体说明", "匿名 fixture"])
        sheet.append(["镜号", "结构", "形式", "内容"])
        sheet.append(["", "", "", "画面示意", "效果示意", "效果", "景别", "运镜", "画面描述", "声音", "备注"])
        sheet.append(common)
        workbook.save(root / filename)

    image_path = root / "reference.png"
    Image.new("RGB", (320, 180), "white").save(image_path)
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    creative = workbook.create_sheet("创意库")
    for sheet in (template, creative):
        sheet.cell(5, 1, "镜号")
        sheet.cell(5, 2, "结构")
        sheet.cell(5, 3, "形式")
        sheet.cell(5, 4, "内容")
        for column, value in enumerate(
            ["画面示意", "效果示意", "效果", "景别", "运镜", "画面描述", "声音", "备注"],
            start=4,
        ):
            sheet.cell(6, column, value)
        for column, value in enumerate(common, start=1):
            sheet.cell(7, column, value)
    creative.add_image(SpreadsheetImage(image_path), "D7")
    workbook.save(root / "M+发布会类通用脚本库.xlsx")


class MvpTests(unittest.TestCase):
    def test_legacy_cli_defaults_use_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "sources"
            with patch.dict(
                os.environ,
                {
                    "MPLUS_CONFIG": str(root / "missing-config.json"),
                    "MPLUS_SOURCE_ROOT": str(source_root),
                    "MPLUS_CODEX_BIN": "/opt/mplus/bin/codex",
                },
                clear=False,
            ):
                self.assertEqual(_default_source_root(), source_root.resolve())
                self.assertEqual(_default_codex_bin(), "/opt/mplus/bin/codex")

    def test_read_briefs_rejects_missing_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.xlsx"
            write_wb(path, [["项目名称", "脚本类型"], ["测试项目", "年会"]])

            with self.assertRaisesRegex(BriefError, "缺少必填字段"):
                read_briefs(path)

    def test_detect_scene_type_prefers_project_filename(self):
        brief = {
            "项目区域文件名": "客户A-发布会-启动仪式",
            "脚本类型": "年会",
        }

        self.assertEqual(detect_scene_type(brief), "发布会")

    def test_load_source_shots_reads_all_five_scene_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source_fixture(root)
            shots = load_source_shots(root)
        by_type = {shot.scene_type for shot in shots}

        self.assertGreaterEqual(len([s for s in shots if s.scene_type == "年会"]), 1)
        self.assertGreaterEqual(len([s for s in shots if s.scene_type == "展会"]), 1)
        self.assertGreaterEqual(len([s for s in shots if s.scene_type == "赛事"]), 1)
        self.assertGreaterEqual(len([s for s in shots if s.scene_type == "音乐节"]), 1)
        self.assertGreaterEqual(len([s for s in shots if s.scene_type == "发布会"]), 1)
        self.assertEqual(by_type, {"年会", "展会", "赛事", "音乐节", "发布会"})

    def test_load_source_shots_inherits_blank_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "M+年会类通用脚本库.xlsx"
            rows = [
                ["M+年会类通用脚本库"],
                ["整体说明", "测试"],
                ["镜号", "结构", "形式", "内容"],
                ["", "", "", "画面示意", "效果示意", "效果", "景别", "运镜", "画面描述", "声音", "备注"],
                [1, "开场", "A", "", "", "闪白", "全景", "前推", "城市地标开场", "音乐起", ""],
                [2, "", "B", "", "", "硬切", "特写", "固定", "品牌 logo 特写", "", ""],
            ]
            write_wb(path, rows)

            shots = load_source_shots(root)

        self.assertEqual([shot.structure for shot in shots], ["开场", "开场"])

    def test_load_source_shots_writes_image_anchor_for_reference_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source_fixture(root)
            shots = load_source_shots(root)

        shot = next(
            s for s in shots
            if s.source_file == "M+发布会类通用脚本库.xlsx"
            and s.source_sheet == "创意库"
            and s.source_row == 7
        )

        self.assertEqual(shot.reference_image_path, "M+发布会类通用脚本库.xlsx#创意库!R7C4")

    def test_select_candidates_limits_to_same_scene_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_source_fixture(root)
            shots = load_source_shots(root)
        brief = {"项目区域文件名": "某品牌年会", "脚本类型": ""}

        selected = select_candidates(brief, shots, limit=40)

        self.assertLessEqual(len(selected), 40)
        self.assertTrue(selected)
        self.assertTrue(all(shot.scene_type == "年会" for shot in selected))

    def test_codex_generator_uses_fake_codex_and_returns_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = tmp_path / "fake_codex.py"
            fake.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import sys
                    out = sys.argv[sys.argv.index("--output-last-message") + 1]
                    data = {"shots": [{
                        "structure": "开场",
                        "duration_seconds": 3,
                        "effect": "硬切",
                        "shot_size": "全景",
                        "camera_movement": "前推",
                        "description": "品牌会场开场",
                        "audio": "音乐起",
                        "note": "",
                        "source_file": "M+年会类通用脚本库.xlsx",
                        "source_sheet": "年会类",
                        "source_row": 5,
                        "reference_image_path": "",
                        "effect_reference_path": "",
                        "match_reason": "同类型参考"
                    }]}
                    open(out, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)
            generator = CodexGenerator(
                codex_bin=str(fake),
                project_root=ROOT,
                schema_path=ROOT / "schemas/codex_shots.schema.json",
                debug_dir=tmp_path / "debug",
            )

            result = generator.generate({"项目名称": "测试", "input_row": 2}, [])

        self.assertEqual(result["shots"][0]["description"], "品牌会场开场")

    def test_build_prompt_requests_sixteen_shots_for_sixty_second_brief(self):
        prompt = build_prompt({"视频时长": "60s"}, [])

        self.assertIn('"target_shot_count": 16', prompt)
        self.assertIn('"target_total_duration_seconds": 60', prompt)

    def test_codex_generator_preserves_debug_files_for_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = tmp_path / "fake_codex.py"
            fake.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import sys
                    out = sys.argv[sys.argv.index("--output-last-message") + 1]
                    open(out, "w", encoding="utf-8").write("not json")
                    """
                ),
                encoding="utf-8",
            )
            fake.chmod(0o755)
            debug = tmp_path / "debug"
            generator = CodexGenerator(
                codex_bin=str(fake),
                project_root=ROOT,
                schema_path=ROOT / "schemas/codex_shots.schema.json",
                debug_dir=debug,
            )

            with self.assertRaises(GenerationError):
                generator.generate({"项目名称": "测试", "input_row": 2}, [])

            self.assertTrue((debug / "row_2_prompt.txt").exists())
            self.assertTrue((debug / "row_2_response.txt").exists())

    def test_write_output_workbook_has_fixed_columns_and_reopens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.xlsx"
            briefs = [{"项目名称": "测试项目", "input_row": 2}]
            generated = [{
                "brief": briefs[0],
                "shots": [{
                    "structure": "开场",
                    "duration_seconds": 3,
                    "effect": "硬切",
                    "shot_size": "全景",
                    "camera_movement": "前推",
                    "description": "品牌会场开场",
                    "audio": "音乐起",
                    "note": "",
                    "source_file": "M+年会类通用脚本库.xlsx",
                    "source_sheet": "年会类",
                    "source_row": 5,
                    "reference_image_path": "",
                    "effect_reference_path": "",
                    "match_reason": "同类型参考",
                }],
            }]

            write_output_workbook(path, generated)
            wb = load_workbook(path)
            ws = wb["脚本"]

            self.assertEqual([cell.value for cell in ws[1]], OUTPUT_COLUMNS)
            self.assertEqual(ws["A2"].value, "测试项目")
            self.assertEqual(ws["K2"].value, "品牌会场开场")
            self.assertEqual(ws["Q2"].value, "年会类")

    def test_write_output_workbook_extracts_image_anchor_to_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.xlsx"
            generated = [{
                "brief": {"项目名称": "测试项目", "input_row": 2},
                "shots": [{
                    "structure": "开场",
                    "duration_seconds": 3,
                    "effect": "硬切",
                    "shot_size": "全景",
                    "camera_movement": "前推",
                    "description": "品牌会场开场",
                    "audio": "音乐起",
                    "note": "",
                    "source_file": "M+发布会类通用脚本库.xlsx",
                    "source_sheet": "创意库",
                    "source_row": 7,
                    "reference_image_path": "M+发布会类通用脚本库.xlsx#创意库!R7C4",
                    "effect_reference_path": "",
                    "match_reason": "同类型参考",
                }],
            }]

            source_root = Path(tmp) / "source"
            write_source_fixture(source_root)
            write_output_workbook(path, generated, source_root=source_root)
            wb = load_workbook(path)
            rel_path = wb["脚本"]["N2"].value

            self.assertTrue(rel_path.startswith("output_assets/"))
            self.assertNotIn("#", rel_path)
            image_path = path.parent / rel_path
            self.assertTrue(image_path.exists())
            self.assertGreater(image_path.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
