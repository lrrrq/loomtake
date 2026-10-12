import tempfile
import unittest
import json
import hashlib
import zipfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from PIL import Image

from mvp_v2.audit import audit_generation, audit_output_workbook_format, audit_rows, write_audit_json
from mvp_v2.auto_pipeline import (
    AutoPipelineError,
    _valid_minimax_rows,
    current_visual_cache_fingerprint,
    prepare_auto_pipeline_assets,
    project_cache_dir,
    project_key_from_brief,
    resolve_project_media_root,
)
from mvp_v2.ai_spec import refresh_ai_spec
from mvp_v2.brand_policy import adapt_source_text_to_project
from mvp_v2.cli import main as cli_main
from mvp_v2.excel_io import (
    AUDIT_COLUMNS,
    OUTPUT_COLUMNS,
    DETAIL_COLUMNS,
    PAIRING_REVIEW_COLUMNS,
    SCRIPT_COLUMNS,
    VISUAL_ASSET_COLUMNS,
    BriefError,
    load_creative_variants,
    load_launch_source_shots,
    load_template_slots,
    project_hint_from_brief,
    read_briefs,
    read_text_brief,
    write_template_output_workbook,
    write_output_workbook,
)
from mvp_v2.generator import (
    generate_script_rows,
    generate_template_rows,
    rewrite_template_rows_from_visual_evidence,
)
from mvp_v2.image_assets import (
    build_image_manifest,
    build_video_frame_asset,
    read_jsonl,
    write_jsonl,
)
from mvp_v2.image_retrieval import search_images_for_slot
from mvp_v2.image_cli import write_minimax_analysis_incremental
from mvp_v2.image_understanding import (
    VisionProviderError,
    _parse_minimax_output,
    audit_image_analysis_provenance,
    generate_api_visual_analysis,
    generate_minimax_visual_analysis,
    generate_minimax_visual_analysis_row,
    import_manual_visual_analysis,
)
from mvp_v2.loop_runner import run_generation_loop, run_generation_once
from mvp_v2.models import SourceShot, TemplateDecision, brief_profile_from_dict
from mvp_v2.scoring import match_template_slots, normalize_structure, score_candidates
from mvp_v2.source_policy import source_reuse_policy
from mvp_v2.template_logic import LAUNCH_SLOT_LOGIC, slot_logic
from mvp_v2.visual_assets import (
    apply_visual_analysis,
    audit_candidate_visual_coverage,
    audit_visual_evidence,
    attach_visual_assets,
    rematch_visual_assets,
    _score_visual_match,
    _visual_allocation_indices,
)
from mvp_v2.visual_review import write_visual_review_html_from_workbook


def write_wb(path, rows, sheet_name="Sheet"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
    wb.save(path)


def write_trusted_visual_cache(cache_dir, manifest_rows, analysis_rows):
    manifest_path = Path(cache_dir) / "image_manifest.jsonl"
    analysis_path = Path(cache_dir) / "image_analysis.minimax.jsonl"
    write_jsonl(manifest_path, manifest_rows)
    write_jsonl(analysis_path, analysis_rows)
    (Path(cache_dir) / "cache_meta.json").write_text(json.dumps({
        "fingerprint": current_visual_cache_fingerprint(),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "analysis_sha256": hashlib.sha256(analysis_path.read_bytes()).hexdigest(),
    }), encoding="utf-8")


def write_fixed_real_brief(path):
    write_wb(path, [[
        "项目名称",
        "项目区域文件名",
        "脚本类型",
        "品牌或客户",
        "行业或活动类别",
        "视频时长",
        "风格调性",
        "必须出现的内容",
        "禁忌或不能出现的内容",
        "素材目录或素材文件",
        "备注",
    ], [
        "美的智美万象发布会开场短片",
        "家电-发布会-【美的智美万象发布会】分镜脚本.xlsx",
        "发布会",
        "美的",
        "家电/科技发布会",
        "60s",
        "硬核科技感，冷峻、纯净、高级，平稳大气",
        "品牌logo、活动主会场、智美万象主题字样、装置空镜、人流互动、核心发布仪式",
        "不要低质感口号堆砌；不要泛泛写品牌宣传片；不要脱离分镜字段",
        "/Users/example/private-fixture.txt",
        "输出要能给团队审核，镜头描述要具体可拍。",
    ]], sheet_name="需求")


FIXED_REAL_BRIEF_TEXT = """美的智美万象发布会开场短片

脚本类型：发布会
项目区域文件名：家电-发布会-【美的智美万象发布会】分镜脚本.xlsx
品牌或客户：美的
行业或活动类别：家电/科技发布会
视频时长：60s
风格调性：硬核科技感，冷峻、纯净、高级，平稳大气
必须出现的内容：品牌logo、活动主会场、智美万象主题字样、装置空镜、人流互动、核心发布仪式
禁忌或不能出现的内容：不要低质感口号堆砌；不要泛泛写品牌宣传片；不要脱离分镜字段
素材目录或素材文件：/Users/example/private-fixture.txt
备注：输出要能给团队审核，镜头描述要具体可拍。
"""


def source_shot(**overrides):
    data = {
        "script_type": "发布会",
        "source_file": "launch.xlsx",
        "source_sheet": "创意库",
        "source_row": 7,
        "shot_no": "1",
        "structure": "开场造势",
        "effect": "字体穿梭",
        "camera_type": "航拍",
        "shot_size": "全景",
        "camera_move": "航拍推进",
        "visual_desc": "品牌 LOGO 穿梭进入活动主会场，呈现硬核科技感",
        "sound": "科技感管弦乐",
        "note": "",
        "reference_image_path": "launch.xlsx#创意库!R7C4",
        "effect_reference_path": "",
        "reuse_level": "",
        "forbidden_note": "",
        "tags": "",
        "shot_function": "",
        "visual_subject": "",
        "brand_neutral": True,
        "brand_neutral_evidence": "人工确认:测试夹具",
        "missing_metadata": ("可复用等级", "禁用说明"),
    }
    data.update(overrides)
    return SourceShot(**data)


class ImagePipelineTests(unittest.TestCase):
    def test_visual_adaptation_does_not_rewrite_template_decision_mode(self):
        script_rows = [{
            "镜号": "1", "画面描述": "模板画面", "时长(秒)": 3, "备注": "",
        }]
        detail_rows = [{
            "slot_index": 1, "模板镜号": "1", "替换方式": "KEEP",
        }]
        visual_assets = [{
            "slot_index": 1,
            "图片角色": "画面示意",
            "配图判断": "匹配",
            "视觉内容摘要": "美的发布会外场主会场",
            "原图路径": "/tmp/visual.png",
            "视觉适配模式": "adapt",
            "视觉适配查询": "主会场",
            "image_id": "img_visual",
        }]

        _, rewritten_details, _ = rewrite_template_rows_from_visual_evidence(
            {"视频时长": "60s"}, script_rows, detail_rows, visual_assets,
        )

        self.assertEqual(rewritten_details[0]["替换方式"], "KEEP")
        self.assertEqual(rewritten_details[0]["视觉适配模式"], "adapt")

    def test_minimax_parser_salvages_arrays_from_malformed_caption_json(self):
        raw = '''```json
{
  "caption": "画面右上角有M+FILMS水印，左上角有"小红书"平台logo。",
  "tags": ["展厅", "科技感"],
  "brand_names": ["M+FILMS", "小红书"],
  "ocr_text": ["M+FILMS", "小红书"]
}
```'''

        parsed = _parse_minimax_output(raw)

        self.assertEqual(parsed["brand_names"], ["M+FILMS", "小红书"])
        self.assertEqual(parsed["tags"], ["展厅", "科技感"])
        self.assertEqual(parsed["ocr_text"], ["M+FILMS", "小红书"])
        self.assertEqual(parsed["caption"], '画面右上角有M+FILMS水印，左上角有"小红书"平台logo。')

    def test_cached_malformed_caption_is_reparsed_before_trust(self):
        raw_caption = '''```json
{
  "caption": "美的舞台左上角有"小红书"logo，右上角有M+FILMS标志",
  "tags": ["发布会", "舞台"],
  "brand_names": ["美的", "小红书", "M+FILMS"]
}
```'''
        rows = [{
            "image_id": "img_cached",
            "caption": raw_caption,
            "tags": ["美的", "舞台"],
            "brand_names": ["美的"],
            "source": "minimax",
        }]

        valid = _valid_minimax_rows(rows, {"img_cached"})

        self.assertEqual(valid[0]["brand_names"], ["美的", "小红书", "M+FILMS"])
        self.assertNotIn("```json", valid[0]["caption"])

    def test_cached_minimax_row_keeps_uncertain_brand_as_analyzed_but_not_trusted(self):
        rows = [{
            "image_id": "img_uncertain",
            "caption": "模糊的发布会舞台与灯光",
            "tags": ["发布会", "舞台"],
            "ocr_text": "无法确认的屏幕文字",
            "brand_names": [],
            "source": "minimax",
        }]

        valid = _valid_minimax_rows(rows, {"img_uncertain"})

        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["brand_names"], ["未知"])

    def test_minimax_row_retries_once_when_brand_verdict_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "image.png"
            Image.new("RGB", (1280, 720), color="white").save(image_path)
            asset = {"image_id": "img_retry", "原图路径": str(image_path)}
            invalid = json.dumps({
                "caption": "家电展厅", "tags": ["家电", "展厅"], "brand_names": [],
            }, ensure_ascii=False)
            valid = json.dumps({
                "caption": "Samsung 家电展厅", "tags": ["家电", "展厅"],
                "brand_names": ["Samsung"],
            }, ensure_ascii=False)

            with patch("mvp_v2.image_understanding._run_minimax_vision", side_effect=[invalid, valid]) as run:
                row = generate_minimax_visual_analysis_row(asset, api_key="test-key")

            self.assertEqual(run.call_count, 2)
            self.assertIn("二次品牌判定", run.call_args_list[1].kwargs["prompt"])
            self.assertEqual(row["brand_names"], ["Samsung"])

    def test_minimax_row_does_not_retry_complete_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "image.png"
            Image.new("RGB", (1280, 720), color="white").save(image_path)
            asset = {"image_id": "img_once", "原图路径": str(image_path)}
            valid = json.dumps({
                "caption": "无品牌发布会现场", "tags": ["发布会"], "brand_names": ["无"],
            }, ensure_ascii=False)

            with patch("mvp_v2.image_understanding._run_minimax_vision", return_value=valid) as run:
                row = generate_minimax_visual_analysis_row(asset, api_key="test-key")

            self.assertEqual(run.call_count, 1)
            self.assertEqual(row["brand_names"], ["无"])

    def test_minimax_row_blocks_asset_when_brand_remains_uncertain_after_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "image.png"
            Image.new("RGB", (1280, 720), color="white").save(image_path)
            asset = {"image_id": "img_uncertain", "原图路径": str(image_path)}
            uncertain = json.dumps({
                "caption": "发布会现场人群欢呼",
                "tags": ["发布会", "人群", "欢呼"],
                "brand_names": ["未知"],
            }, ensure_ascii=False)

            with patch("mvp_v2.image_understanding._run_minimax_vision", return_value=uncertain) as run:
                row = generate_minimax_visual_analysis_row(asset, api_key="test-key")

            self.assertEqual(run.call_count, 2)
            self.assertEqual(row["review_status"], "blocked")
            self.assertIn("品牌不确定禁用", row["negative_flags"])

            checks = audit_image_analysis_provenance([row])
            self.assertNotIn(
                "image_analysis_missing_brand_verdict",
                {check["检查项"] for check in checks},
            )

    def test_visual_allocation_prioritizes_scarce_slots_before_generic_slots(self):
        assets = [{"slot_index": slot} for slot in range(1, 19)]
        ordered_slots = [assets[index]["slot_index"] for index in _visual_allocation_indices(assets)]

        self.assertLess(ordered_slots.index(5), ordered_slots.index(8))
        self.assertLess(ordered_slots.index(13), ordered_slots.index(8))
        self.assertLess(ordered_slots.index(16), ordered_slots.index(15))
        self.assertLess(ordered_slots.index(10), ordered_slots.index(3))

    def test_non_launch_visual_allocation_does_not_inherit_launch_slot_priority(self):
        assets = [{"slot_index": slot} for slot in (15, 3, 22, 1, 9)]

        ordered = _visual_allocation_indices(assets, use_launch_contract=False)

        self.assertEqual(
            [assets[index]["slot_index"] for index in ordered],
            [1, 3, 9, 15, 22],
        )

    def test_auto_pipeline_resolves_stable_project_keys_and_media_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "匿名脚本库"
            for name in ("测试品牌发布会", "测试家居"):
                (source_root / "发布会类" / name).mkdir(parents=True)
            launch = {"项目区域文件名": "【测试品牌发布会】脚本.xlsx"}
            home = {"项目区域文件名": "【测试家居】脚本.xlsx"}

            self.assertEqual(project_key_from_brief(launch), "测试品牌发布会")
            self.assertEqual(project_key_from_brief(home), "测试家居")
            self.assertEqual(resolve_project_media_root(launch, source_root).name, "测试品牌发布会")
            self.assertEqual(resolve_project_media_root(home, source_root).name, "测试家居")

    def test_auto_pipeline_requires_api_key_when_images_are_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "整理后脚本库"
            media = source_root / "发布会类" / "测试项目"
            media.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(media / "01.png")
            brief = {
                "项目名称": "测试发布会",
                "项目区域文件名": "测试项目",
                "素材目录或素材文件": str(media),
            }

            with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=False):
                with self.assertRaisesRegex(AutoPipelineError, "MiniMax 凭据"):
                    prepare_auto_pipeline_assets(
                        brief,
                        source_root,
                        Path(tmp) / "assets",
                        api_key="",
                    )

    def test_auto_pipeline_accepts_authenticated_mmx_without_environment_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "整理后脚本库"
            media = source_root / "发布会类" / "测试项目"
            media.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(media / "01.png")
            brief = {
                "项目名称": "测试发布会",
                "项目区域文件名": "测试项目",
                "素材目录或素材文件": str(media),
            }

            def fake_incremental(manifest_rows, output, **kwargs):
                write_jsonl(output, [{
                    "image_id": manifest_rows[0]["image_id"],
                    "caption": "发布会现场",
                    "tags": ["发布会"],
                    "brand_names": ["无"],
                    "source": "minimax",
                }])
                return 1

            with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=True):
                with patch(
                    "mvp_v2.auto_pipeline.write_minimax_analysis_incremental",
                    side_effect=fake_incremental,
                ) as generate:
                    assets = prepare_auto_pipeline_assets(
                        brief,
                        source_root,
                        Path(tmp) / "assets",
                        api_key="",
                    )

            generate.assert_called_once()
            self.assertEqual(assets.generated_analysis_count, 1)

    def test_auto_pipeline_reuses_complete_minimax_cache_without_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "整理后脚本库"
            media = source_root / "发布会类" / "测试项目"
            media.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(media / "01.png")
            brief = {
                "项目名称": "测试发布会",
                "项目区域文件名": "测试项目",
                "素材目录或素材文件": str(media),
            }
            cache_root = Path(tmp) / "assets"
            cache_dir = project_cache_dir(brief, cache_root)
            manifest = build_image_manifest(media, project_hint="测试项目")
            write_trusted_visual_cache(cache_dir, manifest, [{
                "image_id": manifest[0]["image_id"],
                "caption": "测试发布会品牌logo",
                "tags": ["发布会", "logo"],
                "brand_names": ["无"],
                "source": "minimax",
                "analysis_provider": "minimax",
                "provenance_status": "api_generated",
            }])

            with patch("mvp_v2.auto_pipeline.write_minimax_analysis_incremental") as generate:
                assets = prepare_auto_pipeline_assets(
                    brief,
                    source_root,
                    cache_root,
                    api_key="",
                )

            generate.assert_not_called()
            self.assertEqual(assets.reused_analysis_count, 1)
            self.assertEqual(assets.generated_analysis_count, 0)

    def test_auto_pipeline_generates_only_new_manifest_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "整理后脚本库"
            media = source_root / "发布会类" / "测试项目"
            media.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(media / "01.png")
            Image.new("RGB", (1920, 1080), color="black").save(media / "02.png")
            brief = {
                "项目名称": "测试发布会",
                "项目区域文件名": "测试项目",
                "素材目录或素材文件": str(media),
            }
            cache_root = Path(tmp) / "assets"
            cache_dir = project_cache_dir(brief, cache_root)
            manifest = build_image_manifest(media, project_hint="测试项目")
            write_trusted_visual_cache(cache_dir, manifest, [{
                "image_id": manifest[0]["image_id"],
                "caption": "已分析图片",
                "tags": ["发布会"],
                "brand_names": ["无"],
                "source": "minimax",
            }])

            def fake_incremental(manifest_rows, output, **kwargs):
                existing = read_jsonl(output)
                done = {row["image_id"] for row in existing}
                added = [{
                    "image_id": row["image_id"],
                    "caption": "新增图片视觉分析",
                    "tags": ["发布会"],
                    "brand_names": ["无"],
                    "source": "minimax",
                } for row in manifest_rows if row["image_id"] not in done]
                write_jsonl(output, existing + added)
                return len(added)

            with patch("mvp_v2.auto_pipeline.write_minimax_analysis_incremental", side_effect=fake_incremental) as generate:
                assets = prepare_auto_pipeline_assets(
                    brief,
                    source_root,
                    cache_root,
                    api_key="test-key",
                )

            self.assertEqual(generate.call_count, 1)
            self.assertEqual(assets.reused_analysis_count, 1)
            self.assertEqual(assets.generated_analysis_count, 1)
            self.assertEqual(len(assets.analysis_rows), 2)

    def test_auto_pipeline_merges_video_keyframes_into_project_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "整理后脚本库"
            media = source_root / "发布会类" / "测试项目"
            media.mkdir(parents=True)
            image = media / "01.png"
            frame = Path(tmp) / "video-frame.png"
            Image.new("RGB", (1920, 1080), color="white").save(image)
            Image.new("RGB", (1920, 1080), color="black").save(frame)
            brief = {
                "项目名称": "测试发布会", "品牌或客户": "测试品牌",
                "项目区域文件名": "测试项目", "素材目录或素材文件": str(media),
            }
            video_asset = build_video_frame_asset(
                media / "launch.mp4", frame, "00:00:01.000", 1, project_name="测试项目"
            )

            def analyze_all(manifest_rows, output, **kwargs):
                write_jsonl(output, [{
                    "image_id": row["image_id"], "caption": "发布会视觉",
                    "tags": ["发布会"], "brand_names": ["无"], "source": "minimax",
                } for row in manifest_rows])
                return len(manifest_rows)

            with patch(
                "mvp_v2.auto_pipeline.build_project_video_frame_manifest",
                return_value=[video_asset],
            ):
                with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=True):
                    with patch(
                        "mvp_v2.auto_pipeline.write_minimax_analysis_incremental",
                        side_effect=analyze_all,
                    ):
                        assets = prepare_auto_pipeline_assets(
                            brief, source_root, Path(tmp) / "assets", api_key="test-key"
                        )

            rows = {row["image_id"]: row for row in assets.manifest_rows}
            self.assertIn(video_asset["image_id"], rows)
            self.assertTrue(rows[video_asset["image_id"]]["是否来自视频抽帧"])

    def test_parallel_minimax_analysis_keeps_successes_and_retries_only_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "analysis.jsonl"
            assets = [
                {"image_id": f"img_{index}", "原图路径": str(Path(tmp) / f"{index}.png")}
                for index in range(3)
            ]

            def first_run(asset, *args, **kwargs):
                if asset["image_id"] == "img_1":
                    raise VisionProviderError("temporary failure")
                return {
                    "image_id": asset["image_id"],
                    "caption": "视觉分析",
                    "tags": ["发布会"],
                    "source": "minimax",
                }

            with patch("mvp_v2.image_cli.generate_minimax_visual_analysis_row", side_effect=first_run):
                with self.assertRaisesRegex(VisionProviderError, "部分失败"):
                    write_minimax_analysis_incremental(
                        assets,
                        output,
                        api_key="test-key",
                        workers=2,
                    )
            self.assertEqual({row["image_id"] for row in read_jsonl(output)}, {"img_0", "img_2"})

            with patch("mvp_v2.image_cli.generate_minimax_visual_analysis_row", return_value={
                "image_id": "img_1",
                "caption": "重试成功",
                "tags": ["发布会"],
                "source": "minimax",
            }) as generate:
                added = write_minimax_analysis_incremental(
                    assets,
                    output,
                    api_key="test-key",
                    workers=2,
                )

            self.assertEqual(added, 1)
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(len(read_jsonl(output)), 3)

    def test_build_image_manifest_assigns_stable_ids_and_source_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "美的智美万象发布会" / "01.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(image_path)

            first = build_image_manifest(root, project_hint="美的智美万象发布会")
            second = build_image_manifest(root, project_hint="美的智美万象发布会")

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["image_id"], second[0]["image_id"])
        self.assertEqual(first[0]["原图路径"], str(image_path))
        self.assertEqual(first[0]["项目名"], "美的智美万象发布会")
        self.assertEqual(first[0]["活动类型"], "发布会")
        self.assertEqual(first[0]["尺寸"], "1920x1080")
        self.assertFalse(first[0]["是否低清"])
        self.assertTrue(first[0]["是否来自项目图片池"])
        self.assertFalse(first[0]["是否来自 Excel 嵌图"])
        self.assertFalse(first[0]["是否来自视频抽帧"])

    def test_manual_visual_analysis_import_binds_caption_tags_to_manifest_image_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "美的智美万象发布会" / "07.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")

            analysis = import_manual_visual_analysis(manifest, {
                "07.png": {
                    "summary": "Midea logo 标识特写，画面清晰",
                    "tags": ["Midea", "logo", "品牌标识"],
                    "source": "人工复核",
                }
            })

        self.assertEqual(len(analysis), 1)
        self.assertEqual(analysis[0]["image_id"], manifest[0]["image_id"])
        self.assertEqual(analysis[0]["caption"], "Midea logo 标识特写，画面清晰")
        self.assertIn("logo", analysis[0]["tags"])
        self.assertEqual(analysis[0]["source"], "manual_seed")
        self.assertEqual(analysis[0]["review_status"], "unverified")
        self.assertEqual(analysis[0]["annotator"], "unknown")
        self.assertEqual(analysis[0]["provenance_status"], "missing_provenance")

    def test_manual_visual_analysis_records_annotator_and_source_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "美的智美万象发布会" / "07.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            seed_path = Path(tmp) / "人工标注.json"

            analysis = import_manual_visual_analysis(
                manifest,
                {
                    "_meta": {"annotator": "Codex人工预标注", "annotated_at": "2026-07-08"},
                    "07.png": {
                        "summary": "Midea logo 标识特写，画面清晰",
                        "tags": ["Midea", "logo", "品牌标识"],
                    },
                },
                source_file=seed_path,
            )

        self.assertEqual(analysis[0]["annotator"], "Codex人工预标注")
        self.assertEqual(analysis[0]["annotated_at"], "2026-07-08")
        self.assertEqual(analysis[0]["source_file"], str(seed_path))
        self.assertEqual(analysis[0]["provenance_status"], "ok")

    def test_image_analysis_provenance_audit_flags_unknown_or_unverified_manual_annotator(self):
        checks = audit_image_analysis_provenance([{
            "image_id": "img_1",
            "source": "manual_seed",
            "annotator": "unknown",
            "source_file": "",
            "review_status": "unverified",
            "caption": "Midea logo",
            "tags": ["logo"],
        }])

        items = [check["检查项"] for check in checks]
        self.assertIn("image_analysis_missing_provenance", items)
        self.assertIn("image_analysis_unverified_manual_seed", items)

    def test_image_analysis_provenance_warns_for_uncertain_unused_brand_verdict(self):
        checks = audit_image_analysis_provenance([{
            "image_id": "img_1",
            "source": "minimax",
            "caption": "发布会主会场",
            "tags": ["主会场"],
            "ocr_text": "",
            "brand_names": [],
        }])

        warnings = [check["检查项"] for check in checks if check["级别"] == "warning"]
        self.assertIn("image_analysis_missing_brand_verdict", warnings)

    def test_image_analysis_provenance_does_not_require_brand_for_unusable_blurred_asset(self):
        checks = audit_image_analysis_provenance([{
            "image_id": "img_blurred",
            "source": "minimax",
            "caption": "严重模糊的发布会灯光画面",
            "tags": ["发布会", "模糊"],
            "ocr_text": "无法辨认",
            "brand_names": ["未知"],
            "quality_flags": ["严重模糊"],
            "negative_flags": ["画面模糊无法辨认细节"],
        }])

        self.assertNotIn(
            "image_analysis_missing_brand_verdict",
            {check["检查项"] for check in checks},
        )

    def test_generate_api_visual_analysis_requires_api_key(self):
        with self.assertRaises(VisionProviderError):
            generate_api_visual_analysis([{"image_id": "img_1", "原图路径": "/tmp/missing.png"}], api_key="")

    def test_generate_minimax_visual_analysis_requires_key_or_cli(self):
        with self.assertRaises(VisionProviderError):
            generate_minimax_visual_analysis(
                [{"image_id": "img_1", "原图路径": "/tmp/missing.png"}],
                api_key="",
                command="/tmp/not-existing-mmx",
            )

    def test_generate_minimax_visual_analysis_uses_command_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "01.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            command = Path(tmp) / "fake_mmx.py"
            command.write_text(
                "import sys\n"
                "print('Midea logo 标识特写，蓝色背景，品牌识别清晰')\n",
                encoding="utf-8",
            )

            rows = generate_minimax_visual_analysis(
                [{"image_id": "img_1", "原图路径": str(image_path)}],
                api_key="test-key",
                command=f"python3 {command}",
                model="MiniMax-M3",
            )

        self.assertEqual(rows[0]["image_id"], "img_1")
        self.assertEqual(rows[0]["source"], "minimax")
        self.assertEqual(rows[0]["analysis_model"], "mmx-vision-describe/provider-managed")
        self.assertEqual(rows[0]["requested_model"], "MiniMax-M3")
        self.assertEqual(rows[0]["model_selection"], "provider_managed")
        self.assertIn("Midea logo", rows[0]["caption"])
        self.assertIn("logo", rows[0]["tags"])

    def test_generate_minimax_plain_text_detects_linsy_brand(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "01.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            command = Path(tmp) / "fake_mmx.py"
            command.write_text(
                "print('LINSY 林氏家居品牌logo特写，霓虹蓝粉背景')\n",
                encoding="utf-8",
            )

            rows = generate_minimax_visual_analysis(
                [{"image_id": "img_1", "原图路径": str(image_path)}],
                api_key="test-key",
                command=f"python3 {command}",
            )

        self.assertIn("林氏家居", rows[0]["brand_names"])

    def test_generate_minimax_visual_analysis_parses_mmx_json_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "01.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            command = Path(tmp) / "fake_mmx.py"
            command.write_text(
                "import json\n"
                "print(json.dumps({'content': '```json\\\\n{\\\\\"caption\\\\\":\\\\\"美的品牌logo特写\\\\\",\\\\\"tags\\\\\":[\\\\\"美的\\\\\",\\\\\"logo\\\\\"],\\\\\"brand_names\\\\\":[\\\\\"Midea\\\\\"],\\\\\"scene_type\\\\\":\\\\\"商业展示\\\\\",\\\\\"shot_type\\\\\":\\\\\"特写\\\\\",\\\\\"quality_flags\\\\\":[\\\\\"清晰\\\\\"],\\\\\"negative_flags\\\\\":[]}\\\\n```'}, ensure_ascii=False))\n",
                encoding="utf-8",
            )

            rows = generate_minimax_visual_analysis(
                [{"image_id": "img_1", "原图路径": str(image_path)}],
                api_key="test-key",
                command=f"python3 {command}",
                model="MiniMax-M3",
            )

        self.assertEqual(rows[0]["caption"], "美的品牌logo特写")
        self.assertEqual(rows[0]["tags"], ["美的", "logo"])
        self.assertEqual(rows[0]["brand_names"], ["Midea"])
        self.assertEqual(rows[0]["quality_flags"], ["清晰"])

    def test_generate_minimax_visual_analysis_parses_json_content_with_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "01.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            command = Path(tmp) / "fake_mmx.py"
            command.write_text(
                "import json\n"
                "content='''```json\\n{\\n  \"caption\":\"美的主会场大屏\",\\n  \"tags\":[\"美的\",\"主会场\"],\\n  \"ocr_text\":[\"Midea\"], // 识别到屏幕字样\\n  \"negative_flags\":[]\\n}\\n```'''\n"
                "print(json.dumps({'content': content}, ensure_ascii=False))\n",
                encoding="utf-8",
            )

            rows = generate_minimax_visual_analysis(
                [{"image_id": "img_1", "原图路径": str(image_path)}],
                api_key="test-key",
                command=f"python3 {command}",
            )

        self.assertEqual(rows[0]["caption"], "美的主会场大屏")
        self.assertEqual(rows[0]["tags"], ["美的", "主会场"])

    def test_write_minimax_analysis_incremental_skips_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "01.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            output = Path(tmp) / "analysis.jsonl"
            write_jsonl(output, [{"image_id": "img_1", "caption": "已存在", "tags": ["logo"]}])

            written = write_minimax_analysis_incremental(
                [{"image_id": "img_1", "原图路径": str(image_path)}],
                output,
                api_key="test-key",
                command="python3 -c \"raise SystemExit('should not run')\"",
            )

            self.assertEqual(written, 0)
            self.assertEqual(len(read_jsonl(output)), 1)

    def test_search_images_requires_manifest_analysis_before_auto_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "美的智美万象发布会" / "07.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")

            result = search_images_for_slot(
                {"画面描述": "美的品牌logo穿梭入画"},
                {"项目名称": "美的智美万象发布会开场短片"},
                manifest,
                [],
            )

        self.assertEqual(result["status"], "gap")
        self.assertIn("缺少视觉理解", result["gap_reason"])
        self.assertEqual(result["candidates"], [])

    def test_search_images_filters_wrong_brand_and_blurry_logo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            blurry = pool / "blurry.png"
            wrong_brand = pool / "cleer.png"
            good = pool / "midea_logo.png"
            for path in (blurry, wrong_brand, good):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "blurry.png": {
                    "summary": "Midea logo 虚焦，不够清晰",
                    "tags": ["Midea", "logo", "虚焦"],
                    "source": "人工复核",
                },
                "cleer.png": {
                    "summary": "Cleer 品牌墙前人物互动",
                    "tags": ["Cleer", "品牌墙", "互动"],
                    "source": "人工复核",
                },
                "midea_logo.png": {
                    "summary": "清晰 Midea logo 标识特写",
                    "tags": ["Midea", "logo", "品牌标识", "标识特写"],
                    "source": "人工复核",
                },
            })

            result = search_images_for_slot(
                {"画面描述": "美的品牌logo穿梭入画"},
                {"项目名称": "美的智美万象发布会开场短片"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "midea_logo.png")
        rejected_names = {Path(item["原图路径"]).name for item in result["rejected"]}
        self.assertIn("blurry.png", rejected_names)
        self.assertIn("cleer.png", rejected_names)

    def test_search_images_never_auto_adopts_low_resolution_non_logo_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "测试项目" / "crowd.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (640, 360), color="white").save(image_path)
            manifest = build_image_manifest(root, project_hint="测试项目")
            analysis = import_manual_visual_analysis(manifest, {
                "crowd.png": {
                    "summary": "发布会通道内人群密集参观互动",
                    "tags": ["发布会", "通道", "人群", "参观", "互动"],
                    "brand_names": ["无"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 9, "画面描述": "展馆通道内熙熙攘攘的参观人群"},
                {"项目名称": "测试发布会", "品牌或客户": "测试品牌"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "gap")
        self.assertEqual(result["candidates"], [])

    def test_rematch_clears_low_resolution_bound_asset_when_no_hd_candidate_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "测试项目" / "award.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (640, 360), color="white").save(image_path)
            manifest = build_image_manifest(root, project_hint="测试项目")
            analysis_rows = import_manual_visual_analysis(manifest, {
                "award.png": {
                    "summary": "发布会舞台上嘉宾完成颁发仪式",
                    "tags": ["发布会", "舞台", "嘉宾", "颁发", "仪式"],
                    "brand_names": ["无"],
                },
            })
            script_rows = [{
                "镜号": "15", "画面示意": str(image_path), "效果示意": "",
                "画面描述": "颁发仪式形成正式节点",
            }]
            detail_rows = [{"slot_index": 15, "模板镜号": "15", "替换方式": "REPLACE"}]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            visual_assets[0].update({
                "image_id": manifest[0]["image_id"], "视觉内容摘要": analysis_rows[0]["caption"],
                "视觉标签": "；".join(analysis_rows[0]["tags"]), "brand_names": "无",
                "视觉读取状态": "minimax", "配图判断": "匹配", "视觉匹配分": 95,
            })

            script_rows, detail_rows, visual_assets = rematch_visual_assets(
                script_rows, detail_rows, visual_assets, {}, [image_path],
                manifest_rows=manifest, analysis_rows=analysis_rows,
                brief={"项目名称": "测试发布会", "品牌或客户": "测试品牌"},
            )

        self.assertEqual(script_rows[0]["画面示意"], "")
        self.assertEqual(visual_assets[0]["原图路径"], "")
        self.assertIn("低清", detail_rows[0]["图片绑定说明"])

    def test_slot_one_theme_text_can_use_project_brand_logo_as_visual_adapt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "美的智美万象发布会" / "midea-logo.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "midea-logo.png": {
                    "summary": "美的Midea品牌logo清晰入画",
                    "tags": ["美的", "Midea", "logo", "品牌标识"],
                    "brand_names": ["美的"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 1, "画面描述": "智美万象主题文字沿对角线透视排布"},
                {"项目名称": "美的智美万象发布会", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "midea-logo.png")
        self.assertEqual(result["candidates"][0]["match_mode"], "adapt")

    def test_search_images_allows_provenance_watermarks_but_still_rejects_content_brand(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            neutral = pool / "neutral.png"
            wrong = pool / "colmo.png"
            for path in (neutral, wrong):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "neutral.png": {
                    "summary": "观众在发布会家居体验空间操作智能设备，画面带小红书平台标识和M+FILMS制作水印",
                    "tags": ["发布会", "观众", "家居体验", "智能设备", "操作"],
                    "brand_names": ["小红书", "M+FILMS"],
                },
                "colmo.png": {
                    "summary": "COLMO展台前观众操作智能设备",
                    "tags": ["发布会", "观众", "智能设备", "操作"],
                    "brand_names": ["COLMO", "M+FILMS"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 7, "画面描述": "观众在主题空间交流互动"},
                {"项目名称": "美的智美万象发布会开场短片", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "neutral.png")
        self.assertIn("colmo.png", {Path(item["原图路径"]).name for item in result["rejected"]})

    def test_search_images_filters_alibaba_xiaomi_for_midea_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            wrong = pool / "xiaomi.png"
            good = pool / "midea.png"
            for path in (wrong, good):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "xiaomi.png": {
                    "summary": "小米SU7汽车发布会现场，出现 Xiaomi 与汽车展示",
                    "tags": ["小米", "Xiaomi", "SU7", "汽车"],
                    "brand_names": ["Xiaomi"],
                },
                "midea.png": {
                    "summary": "Midea 美的展区人群互动体验",
                    "tags": ["Midea", "美的", "互动", "体验"],
                    "brand_names": ["Midea"],
                },
            })

            result = search_images_for_slot(
                {"画面描述": "产品互动体验"},
                {"项目名称": "美的智美万象发布会开场短片", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "midea.png")
        self.assertIn("xiaomi.png", {Path(item["原图路径"]).name for item in result["rejected"]})

    def test_search_images_filters_midea_for_linsy_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "林氏家居"
            pool.mkdir(parents=True)
            wrong = pool / "a_midea.png"
            good = pool / "z_linsy.png"
            for path in (wrong, good):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="林氏家居")
            analysis = import_manual_visual_analysis(manifest, {
                "a_midea.png": {
                    "summary": "Midea 美的品牌logo特写",
                    "tags": ["Midea", "美的", "logo"],
                    "brand_names": ["Midea"],
                },
                "z_linsy.png": {
                    "summary": "LINSY 林氏家居品牌logo特写",
                    "tags": ["LINSY", "林氏家居", "logo"],
                    "brand_names": ["LINSY"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 1, "画面描述": "林氏家居品牌logo开场"},
                {"项目名称": "林氏家居生活方式发布会", "品牌或客户": "林氏家居"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "z_linsy.png")
        self.assertIn("a_midea.png", {Path(item["原图路径"]).name for item in result["rejected"]})

    def test_search_images_rejects_visually_blurry_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "林氏家居"
            pool.mkdir(parents=True)
            blurry = pool / "blurry.png"
            clear = pool / "clear.png"
            for path in (blurry, clear):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="林氏家居")
            analysis = import_manual_visual_analysis(manifest, {
                "blurry.png": {
                    "summary": "林氏家居发布会演讲者",
                    "tags": ["林氏家居", "发布会", "演讲者"],
                    "quality_flags": ["虚焦", "严重模糊"],
                },
                "clear.png": {
                    "summary": "林氏家居发布会演讲者清晰中景",
                    "tags": ["林氏家居", "发布会", "演讲者", "清晰"],
                },
            })

            result = search_images_for_slot(
                {"画面描述": "发布会演讲者中景"},
                {"项目名称": "林氏家居生活方式发布会", "品牌或客户": "林氏家居"},
                manifest,
                analysis,
            )

        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "clear.png")
        self.assertIn("blurry.png", {Path(item["原图路径"]).name for item in result["rejected"]})

    def test_search_images_rejects_construction_for_main_venue_empty_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "林氏家居"
            pool.mkdir(parents=True)
            construction = pool / "construction.png"
            venue = pool / "venue.png"
            for path in (construction, venue):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="林氏家居")
            analysis = import_manual_visual_analysis(manifest, {
                "construction.png": {
                    "summary": "林氏家居发布会主会场正在施工搭建",
                    "tags": ["林氏家居", "主会场", "施工", "搭建"],
                },
                "venue.png": {
                    "summary": "林氏家居发布会主会场空镜，舞台大屏与光束清晰可见",
                    "tags": ["林氏家居", "主会场", "空镜", "舞台", "大屏", "光束"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 4, "画面描述": "主会场空镜，多束光束向舞台中心汇聚"},
                {"项目名称": "林氏家居生活方式发布会", "品牌或客户": "林氏家居"},
                manifest,
                analysis,
            )

        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "venue.png")
        self.assertIn("construction.png", {Path(item["原图路径"]).name for item in result["rejected"]})

    def test_search_images_uses_slot_adaptation_when_primary_visual_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            exterior = pool / "exterior.png"
            Image.new("RGB", (1920, 1080), color="white").save(exterior)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "exterior.png": {
                    "summary": "美的全屋智能发布会外场入口，蓝色主视觉与 Midea 立体品牌标识建立活动场地",
                    "tags": ["美的", "Midea", "外场", "入口", "品牌标识"],
                    "brand_names": ["Midea"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 2, "画面描述": "搭建延时：场馆从空旷到搭建完成的缩影"},
                {"项目名称": "美的智美万象发布会开场短片", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(result["candidates"][0]["match_mode"], "adapt")
        self.assertIn("活动场地", result["candidates"][0]["matched_query"])

    def test_search_images_slot5_adapts_to_real_robot_demonstration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            stage = pool / "stage.png"
            robot = pool / "robot.png"
            for path in (stage, robot):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "stage.png": {
                    "summary": "美的发布会主会场蓝色大屏与空观众席",
                    "tags": ["美的", "主会场", "大屏"],
                    "brand_names": ["Midea"],
                },
                "robot.png": {
                    "summary": "美的发布会两个白色机器人操作爆米花机，展示智能设备",
                    "tags": ["美的", "机器人", "操作", "爆米花机", "智能设备展示"],
                    "brand_names": ["Midea"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 5, "画面描述": "签到墙顾客互动"},
                {"项目名称": "美的智美万象发布会开场短片", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "robot.png")
        self.assertEqual(result["candidates"][0]["match_mode"], "adapt")

    def test_search_images_slot5_accepts_registration_form_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "林氏家居" / "registration.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            manifest = build_image_manifest(root, project_hint="林氏家居")
            analysis = import_manual_visual_analysis(manifest, {
                "registration.png": {
                    "summary": "参会者俯身在桌前填写资料，周围观众查看文件",
                    "tags": ["填写资料", "查看文件", "参会者"],
                    "brand_names": ["无"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 5, "画面描述": "签到墙顾客互动"},
                {"项目名称": "林氏家居发布会", "品牌或客户": "林氏家居"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "registration.png")
        self.assertEqual(result["candidates"][0]["match_mode"], "adapt")

    def test_search_images_slot13_requires_phone_recording_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "林氏家居"
            pool.mkdir(parents=True)
            phone = pool / "phone.png"
            crowd = pool / "crowd.png"
            for path in (phone, crowd):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="林氏家居")
            analysis = import_manual_visual_analysis(manifest, {
                "phone.png": {
                    "summary": "发布会前景中观众举起手机拍摄舞台嘉宾",
                    "tags": ["观众", "手机拍摄", "发布会舞台"],
                    "brand_names": ["无"],
                },
                "crowd.png": {
                    "summary": "发布会舞台与满场观众",
                    "tags": ["舞台", "观众", "人群"],
                    "brand_names": ["无"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 13, "画面描述": "观众举起手机录制舞台发布内容"},
                {"项目名称": "林氏家居发布会", "品牌或客户": "林氏家居"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "phone.png")
        self.assertNotIn("crowd.png", {Path(item["原图路径"]).name for item in result["candidates"]})

    def test_search_images_exact_award_evidence_reaches_auto_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "美的智美万象发布会" / "award.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "award.png": {
                    "summary": "发布会舞台上三位人士展示奖牌或证书",
                    "tags": ["三位人士", "奖牌", "证书"],
                    "brand_names": ["无"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 15, "画面描述": "颁发仪式"},
                {"项目名称": "美的智美万象发布会开场短片", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "award.png")

    def test_search_images_treats_award_ceremony_and_medal_as_award_evidence(self):
        match = _score_visual_match(
            "颁发仪式",
            "发布会舞台上三人合影颁奖，中间嘉宾手持奖牌",
            ["颁奖典礼", "奖牌", "舞台"],
        )

        self.assertEqual(match["judgement"], "匹配")
        self.assertIn("颁发/证书", match["hits"])

    def test_search_images_slot17_requires_space_view_not_logo_motion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            logo = pool / "logo-motion.png"
            venue = pool / "venue.png"
            for path in (logo, venue):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "logo-motion.png": {
                    "summary": "Midea品牌logo在屏幕上快速抽离的动态特写",
                    "tags": ["Midea", "logo", "动态特写"],
                    "brand_names": ["Midea"],
                },
                "venue.png": {
                    "summary": "美的发布会主会场全貌，镜头从舞台向后拉远",
                    "tags": ["美的", "主会场", "会场全貌", "拉远", "舞台"],
                    "brand_names": ["美的"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 17, "画面描述": "使用360相机向后拉远至会场全貌"},
                {"项目名称": "美的发布会", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "venue.png")
        self.assertNotIn("logo-motion.png", {Path(item["原图路径"]).name for item in result["candidates"]})

    def test_search_images_slot17_accepts_static_full_venue_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            venue = pool / "venue-wide.png"
            Image.new("RGB", (1920, 1080), color="white").save(venue)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "venue-wide.png": {
                    "summary": "美的发布会沉浸式舞台全景，观众席座无虚席，主舞台为弧形LED大屏",
                    "tags": ["美的", "发布会", "舞台", "观众席", "wide"],
                    "brand_names": ["美的"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 17, "画面描述": "使用360相机向后拉远至会场全貌"},
                {"项目名称": "美的发布会", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "venue-wide.png")

    def test_search_images_slot4_requires_device_when_brief_requires_device_empty_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            generic = pool / "generic-stage.png"
            device = pool / "device-stage.png"
            for path in (generic, device):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "generic-stage.png": {
                    "summary": "美的发布会弧形大屏与蓝色舞台灯光",
                    "tags": ["美的", "舞台", "大屏"],
                    "brand_names": ["美的"],
                },
                "device-stage.png": {
                    "summary": "美的发布会舞台中央的发光圆形装置全景",
                    "tags": ["美的", "舞台", "装置艺术", "全景"],
                    "brand_names": ["美的"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 4, "画面描述": "主会场空镜，舞台光束定场"},
                {
                    "项目名称": "美的发布会", "品牌或客户": "美的",
                    "必须出现的内容": "装置空镜",
                },
                manifest,
                analysis,
            )

        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "device-stage.png")
        self.assertNotIn("generic-stage.png", {Path(item["原图路径"]).name for item in result["candidates"]})

    def test_search_images_slot4_requires_theme_installation_when_brief_requires_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "林氏家居"
            pool.mkdir(parents=True)
            generic = pool / "generic-stage.png"
            theme_installation = pool / "theme-installation.png"
            for path in (generic, theme_installation):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="林氏家居")
            analysis = import_manual_visual_analysis(manifest, {
                "generic-stage.png": {
                    "summary": "林氏家居发布会主会场舞台全景，前方有拱门装饰和LED屏",
                    "tags": ["林氏家居", "发布会", "主会场", "舞台", "LED屏"],
                    "brand_names": ["林氏家居"],
                },
                "theme-installation.png": {
                    "summary": "林氏家居发布会舞台中央城市天际线霓虹主题装置全景",
                    "tags": ["林氏家居", "舞台", "主题装置", "装置艺术", "全景"],
                    "brand_names": ["林氏家居"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 4, "画面描述": "主会场空镜，舞台光束定场"},
                {
                    "项目名称": "林氏家居发布会", "品牌或客户": "林氏家居",
                    "必须出现的内容": "主题装置",
                },
                manifest,
                analysis,
            )

        candidate_names = {Path(item["原图路径"]).name for item in result["candidates"]}
        self.assertIn("theme-installation.png", candidate_names)
        self.assertNotIn("generic-stage.png", candidate_names)

    def test_search_images_slot8_accepts_food_and_drink_service_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            service = pool / "service.png"
            Image.new("RGB", (1920, 1080), color="white").save(service)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "service.png": {
                    "summary": "发布会茶歇服务台摆放饮品与小食，宾客在桌前交流",
                    "tags": ["茶歇", "饮品", "小食", "服务台", "宾客"],
                    "brand_names": ["无"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 8, "画面描述": "茶歇区餐台与饮品服务，宾客往来互动"},
                {"项目名称": "美的发布会", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "service.png")

    def test_search_images_slot7_prefers_product_use_over_generic_immersive_crowd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "林氏家居"
            pool.mkdir(parents=True)
            generic = pool / "generic.png"
            product = pool / "product.png"
            for path in (generic, product):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="林氏家居")
            analysis = import_manual_visual_analysis(manifest, {
                "generic.png": {
                    "summary": "人群涌向霓虹灯双层展台，沉浸式观看投影秀",
                    "tags": ["发布会", "人群", "沉浸式体验", "展台"],
                    "brand_names": ["林氏家居"],
                },
                "product.png": {
                    "summary": "年轻人在儿童房中坐在木马上微笑，周围有帐篷和玩偶",
                    "tags": ["林氏家居", "儿童房", "家居", "微笑", "帐篷", "玩偶"],
                    "brand_names": ["林氏家居"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 7, "画面描述": "客户在主题空间互动"},
                {
                    "项目名称": "林氏家居发布会",
                    "品牌或客户": "林氏家居",
                    "必须出现的内容": "产品体验",
                },
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "product.png")

    def test_search_images_slot7_rejects_service_only_visual_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "美的智美万象发布会" / "service.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "service.png": {
                    "summary": "机器人向嘉宾递送咖啡饮品，现场观众围观互动",
                    "tags": ["机器人", "咖啡", "饮品", "嘉宾互动"],
                    "brand_names": ["无"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 7, "画面描述": "客户在主题空间体验产品"},
                {"项目名称": "美的发布会", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "gap")
        self.assertIn("镜头功能", result["rejected"][0]["reason"])

    def test_search_images_slot8_accepts_dining_social_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "林氏家居"
            pool.mkdir(parents=True)
            dining = pool / "dining.png"
            Image.new("RGB", (1920, 1080), color="white").save(dining)
            manifest = build_image_manifest(root, project_hint="林氏家居")
            analysis = import_manual_visual_analysis(manifest, {
                "dining.png": {
                    "summary": "发布会现场木质长桌摆满菜品和餐具，几位嘉宾围坐交谈",
                    "tags": ["发布会", "聚餐", "菜品", "餐桌", "嘉宾交流"],
                    "brand_names": ["林氏家居"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 8, "画面描述": "现场服务与社交氛围，宾客往来互动"},
                {"项目名称": "林氏家居发布会", "品牌或客户": "林氏家居"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "dining.png")

    def test_visual_allocation_prioritizes_narrow_service_slot_before_broad_experience_slot(self):
        assets = [{"slot_index": 7}, {"slot_index": 8}]

        ordered = _visual_allocation_indices(assets)

        self.assertEqual([assets[index]["slot_index"] for index in ordered], [8, 7])

    def test_search_images_slot18_requires_brief_theme_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            generic = pool / "generic.png"
            exact = pool / "exact.png"
            for path in (generic, exact):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "generic.png": {
                    "summary": "美的发布会蓝色品牌宣传图",
                    "tags": ["美的", "品牌宣传", "主视觉"],
                    "brand_names": ["美的"],
                },
                "exact.png": {
                    "summary": "美的发布会智美万象主视觉KV",
                    "tags": ["美的", "智美万象", "主视觉", "KV"],
                    "brand_names": ["美的"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 18, "画面描述": "END落版主视觉KV"},
                {
                    "项目名称": "美的发布会", "品牌或客户": "美的",
                    "必须出现的内容": "智美万象主题字样",
                },
                manifest,
                analysis,
            )

        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "exact.png")
        self.assertNotIn("generic.png", {Path(item["原图路径"]).name for item in result["candidates"]})

    def test_search_images_slot18_requires_actual_slate_not_blue_crowd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            crowd = pool / "crowd.png"
            slate = pool / "slate.png"
            for path in (crowd, slate):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "crowd.png": {
                    "summary": "拥挤的蓝色主题展区，观众在长桌前领取物品",
                    "tags": ["蓝色主题", "人群", "展区"],
                    "brand_names": ["无"],
                },
                "slate.png": {
                    "summary": "2026美的全屋智能发布会智美万象主题宣传图",
                    "tags": ["美的", "智美万象", "主视觉", "宣传图", "KV"],
                    "brand_names": ["美的"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 18, "画面描述": "END画面变白，浮现智美万象主视觉KV"},
                {"项目名称": "美的发布会", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "slate.png")
        self.assertNotIn("crowd.png", {Path(item["原图路径"]).name for item in result["candidates"]})

    def test_search_images_slot15_adapts_to_formal_stage_ceremony(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "林氏家居" / "ceremony.png"
            speaker_path = root / "发布会类" / "林氏家居" / "speaker.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            Image.new("RGB", (1920, 1080), color="white").save(speaker_path)
            manifest = build_image_manifest(root, project_hint="林氏家居")
            analysis = import_manual_visual_analysis(manifest, {
                "ceremony.png": {
                    "summary": "林氏家居发布会多位嘉宾在舞台集体登台完成正式仪式",
                    "tags": ["林氏家居", "嘉宾", "舞台", "集体登台", "正式仪式", "掌声"],
                },
                "speaker.png": {
                    "summary": "林氏家居发布会演讲嘉宾站在舞台讲台前发言",
                    "tags": ["林氏家居", "演讲嘉宾", "舞台", "发言"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 15, "画面描述": "颁发仪式"},
                {"项目名称": "林氏家居生活方式发布会", "品牌或客户": "林氏家居"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(result["candidates"][0]["match_mode"], "adapt")
        self.assertIn("speaker.png", {Path(item["原图路径"]).name for item in result["rejected"]})

    def test_search_images_slot6_prefers_guest_evidence_over_empty_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            stage = pool / "a_stage.png"
            speaker = pool / "b_speaker.png"
            car = pool / "c_car.png"
            guests = pool / "z_guests.png"
            for path in (stage, speaker, car, guests):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "a_stage.png": {
                    "summary": "美的发布会主会场蓝色大屏与空观众席",
                    "tags": ["美的", "主会场", "大屏", "空镜"],
                },
                "b_speaker.png": {
                    "summary": "美的发布会演讲嘉宾站在舞台讲台前发言",
                    "tags": ["美的", "演讲嘉宾", "舞台", "发言"],
                },
                "c_car.png": {
                    "summary": "两位男士在汽车前排座舱内交流车内功能",
                    "tags": ["汽车", "车内", "驾驶位", "交流"],
                },
                "z_guests.png": {
                    "summary": "美的发布会三位嘉宾在圆环装置前交流，摄影团队记录现场",
                    "tags": ["美的", "三位嘉宾", "交流", "摄影团队", "圆环装置"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 6, "画面描述": "高管团合影墙打卡"},
                {"项目名称": "美的智美万象发布会开场短片", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "z_guests.png")
        self.assertEqual(result["candidates"][0]["match_mode"], "adapt")
        rejected = {Path(item["原图路径"]).name for item in result["rejected"]}
        self.assertIn("b_speaker.png", rejected)
        self.assertIn("c_car.png", rejected)

    def test_search_images_rejects_cartoon_emoji_for_real_interaction_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            cartoon = pool / "emoji.png"
            real = pool / "robot.png"
            for path in (cartoon, real):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "emoji.png": {"summary": "卡通表情包飞吻动图", "tags": ["卡通", "表情包", "飞吻", "互动"], "brand_names": ["无"]},
                "robot.png": {"summary": "机器人咖啡亭与嘉宾互动，观众围观拍摄", "tags": ["机器人", "咖啡", "互动", "观众"], "brand_names": ["无"]},
            })

            result = search_images_for_slot(
                {"画面描述": "机器人咖啡亭向嘉宾递送咖啡，现场观众围观拍摄"},
                {"项目名称": "美的智美万象发布会开场短片", "品牌或客户": "美的"},
                manifest,
                analysis,
            )

        self.assertEqual(Path(result["candidates"][0]["原图路径"]).name, "robot.png")
        self.assertIn("emoji.png", {Path(item["原图路径"]).name for item in result["rejected"]})

    def test_search_images_does_not_apply_global_brief_musts_to_every_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            image_path = root / "发布会类" / "美的智美万象发布会" / "logo.png"
            image_path.parent.mkdir(parents=True)
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis = import_manual_visual_analysis(manifest, {
                "logo.png": {
                    "summary": "清晰 Midea logo 标识特写",
                    "tags": ["Midea", "logo", "品牌标识"],
                    "source": "人工复核",
                }
            })

            result = search_images_for_slot(
                {"画面描述": "美的品牌logo穿梭入画"},
                {
                    "项目名称": "美的智美万象发布会开场短片",
                    "必须出现的内容": "品牌logo、活动主会场、人流互动、核心发布仪式、大合影",
                },
                manifest,
                analysis,
            )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(result["candidates"][0]["missing"], [])

    def test_visual_match_accepts_minimax_synonyms_for_launch_event_assets(self):
        match = _score_visual_match(
            "2026美的全屋智能战略发布会蓝白主视觉KV，智美万象主题与Midea品牌落版",
            "2026美的全屋智能战略发布会宣传图，整体采用蓝白配色，画面中央有微笑的圆形吉祥物",
            ["发布会", "智能家居", "品牌宣传", "蓝色主题", "企业形象"],
        )

        self.assertEqual(match["judgement"], "匹配")
        self.assertNotIn("主视觉/KV/END", match["missing"])

    def test_visual_match_treats_brand_mark_as_logo(self):
        match = _score_visual_match(
            "Midea品牌标志蓝白特写，开场先建立品牌识别",
            "Midea品牌标志的特写画面，蓝白配色，带有金属反光质感",
            ["品牌标志", "发布会", "特写镜头", "蓝色背景", "logo"],
        )

        self.assertEqual(match["judgement"], "匹配")
        self.assertIn("logo/品牌标识", match["hits"])

    def test_visual_match_rejects_award_stage_as_final_kv(self):
        match = _score_visual_match(
            "END 画面变白，浮现智美万象主视觉 KV",
            "美的发布会舞台上三位嘉宾展示证书，大屏写着全球智能家电销量第一品牌",
            ["颁奖", "证书", "三位嘉宾", "第一品牌"],
        )

        self.assertEqual(match["judgement"], "不匹配")
        self.assertIn("片尾主视觉", "；".join(match["conflicts"]))

    def test_visual_match_accepts_launch_ball_device_for_start_ceremony(self):
        match = _score_visual_match(
            "嘉宾共同完成仪式启动，形成高潮",
            "多位嘉宾在舞台上共同触碰发光球装置，完成启动仪式",
            ["嘉宾", "共同启动", "发光球", "舞台"],
        )

        self.assertEqual(match["judgement"], "匹配")
        self.assertIn("启动/大合影", match["hits"])
        self.assertNotIn("展区打卡", "；".join(match["conflicts"]))

    def test_slot16_core_launch_ceremony_rejects_generic_toast(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "林氏家居"
            pool.mkdir(parents=True)
            toast = pool / "toast.png"
            startup = pool / "startup.png"
            for path in (toast, startup):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="林氏家居")
            analysis = import_manual_visual_analysis(manifest, {
                "toast.png": {
                    "summary": "林氏家居发布会多位嘉宾举杯共庆，现场掌声欢呼",
                    "tags": ["林氏家居", "嘉宾", "举杯", "掌声", "欢呼"],
                    "brand_names": ["林氏家居"],
                },
                "startup.png": {
                    "summary": "林氏家居发布会多位嘉宾共同触碰发光球启动装置",
                    "tags": ["林氏家居", "嘉宾", "共同启动", "发光球", "启动仪式"],
                    "brand_names": ["林氏家居"],
                },
            })

            result = search_images_for_slot(
                {"slot_index": 16, "画面描述": "核心发布仪式形成高潮"},
                {
                    "项目名称": "林氏家居发布会", "品牌或客户": "林氏家居",
                    "必须出现的内容": "核心发布仪式",
                },
                manifest,
                analysis,
            )

        candidate_names = {Path(item["原图路径"]).name for item in result["candidates"]}
        self.assertIn("startup.png", candidate_names)
        self.assertNotIn("toast.png", candidate_names)

    def test_visual_match_rejects_midea_asset_for_linsy_script(self):
        match = _score_visual_match(
            "林氏家居品牌logo开场建立品牌识别",
            "Midea美的品牌logo特写",
            ["Midea", "美的", "logo"],
        )

        self.assertEqual(match["judgement"], "不匹配")
        self.assertIn("其他品牌", "；".join(match["conflicts"]))

    def test_jsonl_roundtrip_for_manifest_and_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image_manifest.jsonl"
            rows = [{"image_id": "img_1", "原图路径": "/tmp/01.png"}]

            write_jsonl(path, rows)
            loaded = read_jsonl(path)

        self.assertEqual(loaded, rows)

    def test_rematch_visual_assets_uses_manifest_analysis_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            bad = pool / "bad.png"
            good = pool / "good_logo.png"
            Image.new("RGB", (1920, 1080), color="white").save(bad)
            Image.new("RGB", (1920, 1080), color="white").save(good)
            script_rows = [{
                "镜号": "1",
                "画面示意": str(bad),
                "效果示意": "",
                "画面描述": "美的品牌logo穿梭入画",
            }]
            detail_rows = [{
                "slot_index": 1,
                "模板镜号": "1",
                "项目名称": "美的智美万象发布会开场短片",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 8,
                "输出图片来源": "创意库",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            legacy_analysis = {
                "bad.png": {"summary": "外场建筑与门头", "tags": ["外场", "建筑"], "source": "人工复核"}
            }
            detail_rows, visual_assets = apply_visual_analysis(script_rows, detail_rows, visual_assets, legacy_analysis)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            analysis_rows = import_manual_visual_analysis(manifest, {
                "good_logo.png": {
                    "summary": "清晰 Midea logo 标识特写",
                    "tags": ["Midea", "logo", "品牌标识"],
                    "source": "人工复核",
                }
            })

            script_rows, detail_rows, visual_assets = rematch_visual_assets(
                script_rows,
                detail_rows,
                visual_assets,
                legacy_analysis,
                [bad, good],
                manifest_rows=manifest,
                analysis_rows=analysis_rows,
                brief={"项目名称": "美的智美万象发布会开场短片"},
            )

        self.assertEqual(script_rows[0]["画面示意"], str(good))
        self.assertTrue(visual_assets[0]["image_id"].startswith("img_"))
        self.assertEqual(detail_rows[0]["image_id"], visual_assets[0]["image_id"])

    def test_rematch_visual_assets_clears_untrusted_image_not_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            generic = Path(tmp) / "generic.png"
            Image.new("RGB", (1920, 1080), color="white").save(generic)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            script_rows = [{
                "镜号": "2",
                "画面示意": str(generic),
                "效果示意": "",
                "画面描述": "搭建延时：场馆从空旷到搭建完成的缩影",
            }]
            detail_rows = [{
                "slot_index": 2,
                "模板镜号": "2",
                "项目名称": "美的智美万象发布会开场短片",
                "替换方式": "REPLACE",
                "图片证据类型": "模板回退",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)

            script_rows, detail_rows, visual_assets = rematch_visual_assets(
                script_rows,
                detail_rows,
                visual_assets,
                {},
                [generic],
                manifest_rows=manifest,
                analysis_rows=[],
                brief={"项目名称": "美的智美万象发布会开场短片", "品牌或客户": "美的"},
            )

        self.assertEqual(script_rows[0]["画面示意"], "")
        self.assertEqual(visual_assets[0]["原图路径"], "")
        self.assertEqual(visual_assets[0].get("image_id", ""), "")
        self.assertEqual(detail_rows[0].get("image_id", ""), "")
        self.assertEqual(visual_assets[0]["配图判断"], "不匹配")
        self.assertIn("未入库", detail_rows[0]["图片绑定说明"])

    def test_rematch_visual_assets_honors_loop_exclusion_even_when_semantic_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            excluded = pool / "excluded.png"
            Image.new("RGB", (1920, 1080), color="white").save(excluded)
            manifest = build_image_manifest(pool, project_hint="美的智美万象发布会")
            image_id = manifest[0]["image_id"]
            analysis_rows = [{
                "image_id": image_id,
                "caption": "美的智美万象主视觉KV",
                "tags": ["美的", "主视觉", "KV"],
                "brand_names": ["美的"],
                "source": "minimax",
            }]
            script_rows = [{
                "镜号": "18",
                "画面示意": str(excluded),
                "效果示意": "",
                "画面描述": "美的智美万象主视觉KV落版",
            }]
            detail_rows = [{
                "slot_index": 18,
                "模板镜号": "18",
                "项目名称": "美的智美万象发布会",
                "替换方式": "KEEP",
                "输出图片来源": "模版",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            detail_rows, visual_assets = apply_visual_analysis(
                script_rows,
                detail_rows,
                visual_assets,
                {excluded.name: {
                    "summary": "美的智美万象主视觉KV",
                    "tags": ["美的", "主视觉", "KV"],
                    "source": "minimax",
                }},
            )

            script_rows, detail_rows, visual_assets = rematch_visual_assets(
                script_rows,
                detail_rows,
                visual_assets,
                {},
                [excluded],
                manifest_rows=manifest,
                analysis_rows=analysis_rows,
                brief={"项目名称": "美的智美万象发布会", "品牌或客户": "美的"},
                visual_excludes_by_slot={"18": [image_id, str(excluded)]},
            )

        self.assertEqual(script_rows[0]["画面示意"], "")
        self.assertEqual(visual_assets[0]["原图路径"], "")
        self.assertIn("上一轮视觉审计", detail_rows[0]["图片绑定说明"])

    def test_rematch_visual_assets_replaces_current_function_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            service = pool / "service.png"
            product = pool / "product.png"
            for path in (service, product):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            manifest_by_name = {Path(row["原图路径"]).name: row for row in manifest}
            analysis_rows = import_manual_visual_analysis(manifest, {
                "service.png": {
                    "summary": "机器人向嘉宾递送咖啡饮品，现场观众围观",
                    "tags": ["机器人", "咖啡", "饮品", "嘉宾"],
                    "brand_names": ["无"],
                },
                "product.png": {
                    "summary": "参会者在家居体验区操作智能设备和互动装置",
                    "tags": ["家居体验", "操作", "智能设备", "互动装置"],
                    "brand_names": ["无"],
                },
            })
            script_rows = [{
                "镜号": "7",
                "画面示意": str(service),
                "效果示意": "",
                "画面描述": "客户在主题空间体验产品",
            }]
            detail_rows = [{
                "slot_index": 7,
                "模板镜号": "7",
                "项目名称": "美的发布会",
                "替换方式": "KEEP",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            current = visual_assets[0]
            current.update({
                "image_id": manifest_by_name["service.png"]["image_id"],
                "视觉内容摘要": "机器人向嘉宾递送咖啡饮品，现场观众围观",
                "视觉标签": "机器人；咖啡；饮品；嘉宾",
                "视觉读取状态": "minimax",
                "配图判断": "匹配",
                "视觉匹配分": 90,
            })

            script_rows, detail_rows, visual_assets = rematch_visual_assets(
                script_rows,
                detail_rows,
                visual_assets,
                {},
                [service, product],
                manifest_rows=manifest,
                analysis_rows=analysis_rows,
                brief={"项目名称": "美的发布会", "品牌或客户": "美的"},
            )

        self.assertEqual(Path(script_rows[0]["画面示意"]).name, "product.png")

    def test_rematch_visual_assets_replaces_current_wrong_brand_even_when_function_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "整理后脚本库"
            pool = root / "发布会类" / "美的智美万象发布会"
            pool.mkdir(parents=True)
            wrong_brand = pool / "linsy-logo.png"
            project_brand = pool / "midea-logo.png"
            for path in (wrong_brand, project_brand):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            manifest = build_image_manifest(root, project_hint="美的智美万象发布会")
            manifest_by_name = {Path(row["原图路径"]).name: row for row in manifest}
            analysis_rows = import_manual_visual_analysis(manifest, {
                "linsy-logo.png": {
                    "summary": "林氏家居品牌logo清晰入画",
                    "tags": ["林氏家居", "logo", "品牌标识"],
                    "brand_names": ["林氏家居"],
                },
                "midea-logo.png": {
                    "summary": "美的Midea品牌logo清晰入画",
                    "tags": ["美的", "Midea", "logo", "品牌标识"],
                    "brand_names": ["美的"],
                },
            })
            script_rows = [{
                "镜号": "1",
                "画面示意": str(wrong_brand),
                "效果示意": "",
                "画面描述": "美的品牌logo穿梭入画",
            }]
            detail_rows = [{
                "slot_index": 1,
                "模板镜号": "1",
                "项目名称": "美的发布会",
                "替换方式": "REPLACE",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            visual_assets[0].update({
                "image_id": manifest_by_name["linsy-logo.png"]["image_id"],
                "视觉内容摘要": "林氏家居品牌logo清晰入画",
                "视觉标签": "林氏家居；logo；品牌标识",
                "brand_names": "林氏家居",
                "视觉读取状态": "minimax",
                "配图判断": "匹配",
                "视觉匹配分": 95,
            })

            script_rows, detail_rows, visual_assets = rematch_visual_assets(
                script_rows,
                detail_rows,
                visual_assets,
                {},
                [wrong_brand, project_brand],
                manifest_rows=manifest,
                analysis_rows=analysis_rows,
                brief={"项目名称": "美的发布会", "品牌或客户": "美的"},
            )

        self.assertEqual(Path(script_rows[0]["画面示意"]).name, "midea-logo.png")
        self.assertNotIn("林氏家居", str(visual_assets[0].get("brand_names") or ""))

    def test_rematch_visual_assets_never_preserves_low_res_wrong_brand_for_higher_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrong_brand = Path(tmp) / "linsy-logo.png"
            project_brand = Path(tmp) / "midea-logo.png"
            Image.new("RGB", (640, 360), color="white").save(wrong_brand)
            Image.new("RGB", (1920, 1080), color="white").save(project_brand)
            script_rows = [{
                "镜号": "1",
                "画面示意": str(wrong_brand),
                "效果示意": "",
                "画面描述": "美的品牌logo穿梭入画",
            }]
            detail_rows = [{"slot_index": 1, "模板镜号": "1", "替换方式": "REPLACE"}]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            visual_assets[0].update({
                "image_id": "wrong-image",
                "视觉内容摘要": "林氏家居品牌logo清晰入画",
                "视觉标签": "林氏家居；logo；品牌标识",
                "brand_names": "林氏家居",
                "视觉读取状态": "minimax",
                "配图判断": "匹配",
                "视觉匹配分": 100,
            })
            lower_score_project_candidate = {
                "path": project_brand,
                "image_id": "project-image",
                "score": 90,
                "selection_score": 90,
                "summary": "美的Midea品牌logo入画",
                "tags": ["美的", "Midea", "logo", "品牌标识"],
                "ocr_text": "Midea",
                "brand_names": ["美的"],
                "negative_flags": [],
                "source": "minimax",
                "judgement": "匹配",
                "confidence": "高",
                "hits": ["logo/品牌标识"],
                "missing": [],
                "conflicts": [],
                "match_reason": "当前项目品牌与槽位职责匹配",
            }

            with patch(
                "mvp_v2.visual_assets._best_manifest_candidate",
                return_value=lower_score_project_candidate,
            ):
                script_rows, _, visual_assets = rematch_visual_assets(
                    script_rows,
                    detail_rows,
                    visual_assets,
                    {},
                    [wrong_brand, project_brand],
                    manifest_rows=[{"image_id": "wrong-image", "原图路径": str(wrong_brand)}],
                    analysis_rows=[{"image_id": "wrong-image", "caption": "x", "tags": ["logo"]}],
                    brief={"项目名称": "美的发布会", "品牌或客户": "美的"},
                )

        self.assertEqual(Path(script_rows[0]["画面示意"]).name, "midea-logo.png")
        self.assertEqual(visual_assets[0]["image_id"], "project-image")

    def test_rematch_visual_assets_never_preserves_low_res_function_mismatch_for_higher_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            toast = Path(tmp) / "toast.png"
            startup = Path(tmp) / "startup.png"
            Image.new("RGB", (640, 360), color="white").save(toast)
            Image.new("RGB", (1920, 1080), color="white").save(startup)
            script_rows = [{
                "镜号": "16", "画面示意": str(toast), "效果示意": "",
                "画面描述": "核心发布仪式形成高潮",
            }]
            detail_rows = [{"slot_index": 16, "模板镜号": "16", "替换方式": "REPLACE"}]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            visual_assets[0].update({
                "image_id": "toast-image",
                "视觉内容摘要": "多位嘉宾举杯共庆，气氛热烈",
                "视觉标签": "举杯；庆祝；嘉宾",
                "brand_names": "林氏家居",
                "视觉读取状态": "minimax",
                "配图判断": "匹配",
                "视觉匹配分": 100,
            })
            startup_candidate = {
                "path": startup,
                "image_id": "startup-image",
                "score": 90,
                "selection_score": 90,
                "summary": "多位嘉宾共同触碰发光球启动装置",
                "tags": ["嘉宾", "共同启动", "发光球", "启动仪式"],
                "ocr_text": "",
                "brand_names": ["林氏家居"],
                "negative_flags": [],
                "source": "minimax",
                "judgement": "匹配",
                "confidence": "高",
                "hits": ["启动/大合影"],
                "missing": [],
                "conflicts": [],
                "match_reason": "启动仪式职责匹配",
            }

            with patch("mvp_v2.visual_assets._best_manifest_candidate", return_value=startup_candidate):
                script_rows, _, visual_assets = rematch_visual_assets(
                    script_rows,
                    detail_rows,
                    visual_assets,
                    {},
                    [toast, startup],
                    manifest_rows=[{"image_id": "toast-image", "原图路径": str(toast)}],
                    analysis_rows=[{"image_id": "toast-image", "caption": "x", "tags": ["庆祝"]}],
                    brief={"项目名称": "林氏家居发布会", "品牌或客户": "林氏家居"},
                )

        self.assertEqual(Path(script_rows[0]["画面示意"]).name, "startup.png")
        self.assertEqual(visual_assets[0]["image_id"], "startup-image")


def write_launch_library(path, template_rows, creative_rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "模版"
    for row in [
        ["M+发布会类通用模版"],
        ["整体说明", "测试"],
        [],
        [],
        ["结构", "镜号", "内容"],
        [None, None, "画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注"],
    ] + template_rows:
        ws.append(row)
    ws.merge_cells("A1:K1")
    ws.merge_cells("A5:A6")
    ws.merge_cells("B5:B6")
    ws.merge_cells("C5:K5")
    ws.column_dimensions["C"].width = 94
    ws.column_dimensions["I"].width = 67
    for row_no in range(7, 7 + len(template_rows)):
        ws.row_dimensions[row_no].height = 148.5
    creative = wb.create_sheet("创意库")
    for row in [
        ["M+发布会类通用脚本库"],
        ["整体说明", "测试"],
        [],
        [],
        ["镜号", "结构", "形式", "内容"],
        [None, None, None, "画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"],
    ] + creative_rows:
        creative.append(row)
    wb.save(path)


def full_launch_template_rows():
    descriptions = {
        1: "品牌logo穿梭入画",
        2: "活动主会场全景建立空间",
        4: "主题装置空镜展示空间设计",
        5: "人流互动与观众交流",
        12: "核心发布仪式形成高潮",
        18: "智美万象主题字样与品牌logo收束",
    }
    structures = {
        1: "开场造势",
        2: "开场造势",
        3: "核心宣讲与仪式",
        4: "核心宣讲与仪式",
        5: "人气与现场互动",
        6: "人气与现场互动",
        7: "人气与现场互动",
        8: "人气与现场互动",
        9: "人气与现场互动",
        10: "核心宣讲与仪式",
        11: "核心宣讲与仪式",
        12: "核心宣讲与仪式",
        13: "核心宣讲与仪式",
        14: "核心宣讲与仪式",
        15: "核心宣讲与仪式",
        16: "核心宣讲与仪式",
        17: "收束",
        18: "收束",
    }
    return [
        [
            structures[index], index, None, None, "字体穿梭", "航拍", "全景", "固定",
            descriptions.get(index, "发布会现场细节镜头"), "", "",
        ]
        for index in range(1, 19)
    ]


def write_launch_media(root, *names):
    media_dir = Path(root) / "发布会类" / "美的智美万象发布会"
    media_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        Image.new("RGB", (1920, 1080), color="white").save(media_dir / name)


def write_full_launch_media(root):
    write_launch_media(root, *[f"{index:02d}.png" for index in range(1, 19)])


def anonymous_launch_template(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_full_launch_media(root)
    path = root / "M+发布会类通用脚本库.xlsx"
    write_launch_library(path, full_launch_template_rows(), [])
    return path


def full_launch_vision_analysis():
    return {
        f"{index:02d}.png": {
            "summary": full_launch_visual_evidence(index)[0],
            "tags": full_launch_visual_evidence(index)[1],
            "brand_names": ["Midea"],
            "source": "minimax",
        }
        for index in range(1, 19)
    }


def full_launch_visual_evidence(index):
    evidence = {
        1: ("Midea品牌logo清晰特写", ["Midea", "logo", "品牌标识"]),
        2: ("美的发布会外场入口与建筑全景", ["Midea", "外场", "建筑", "入口"]),
        3: ("Midea品牌logo动效特写", ["Midea", "logo", "品牌标识"]),
        4: ("美的发布会舞台中央发光主题装置空镜", ["Midea", "舞台", "装置空镜"]),
        5: ("观众在美的发布会签到墙登记打卡", ["Midea", "观众", "签到", "打卡墙"]),
        6: ("发布会嘉宾在品牌空间交流", ["Midea", "嘉宾", "交流"]),
        7: ("顾客操作智能设备进行产品互动体验", ["Midea", "产品体验", "操作设备"]),
        8: ("茶歇餐台摆放饮品小食，宾客交流", ["Midea", "茶歇", "餐台", "宾客"]),
        9: ("发布会通道人群参观交流", ["Midea", "人群", "参观"]),
        10: ("Midea品牌logo回扣转场", ["Midea", "logo", "品牌标识"]),
        11: ("美的发布会主会场舞台与LED大屏", ["Midea", "主会场", "舞台", "大屏"]),
        12: ("演讲者在发布会大屏前讲解战略", ["Midea", "演讲者", "大屏"]),
        13: ("观众用手机录制发布会舞台", ["Midea", "观众", "手机", "录屏"]),
        14: ("发布会舞台中场表演", ["Midea", "舞台", "表演"]),
        15: ("嘉宾在舞台颁奖并手持证书", ["Midea", "颁奖", "证书", "正式仪式"]),
        16: ("多位嘉宾共同触碰启动球完成启动仪式", ["Midea", "嘉宾", "启动", "启动仪式"]),
        17: ("发布会舞台与观众席会场全景", ["Midea", "舞台全景", "会场全貌"]),
        18: ("美的智美万象主题主视觉KV落版", ["Midea", "智美万象", "主视觉", "KV"]),
    }
    return evidence[index]


class MvpV2Tests(unittest.TestCase):
    def test_launch_audit_accepts_seventeen_slot_template(self):
        canonical_roles = [*range(1, 10), *range(11, 19)]
        slots = []
        for actual_slot, logic_slot in enumerate(canonical_roles, start=1):
            logic = LAUNCH_SLOT_LOGIC[logic_slot]
            slots.append(source_shot(
                source_sheet="模版",
                source_row=6 + actual_slot,
                slot_index=actual_slot,
                shot_no=str(actual_slot),
                visual_desc=logic.preferred_terms[0],
                effect="",
                note="",
            ))
        decisions = [
            TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板")
            for slot in slots
        ]
        brief = {
            "项目名称": "17镜发布会",
            "脚本类型": "发布会",
            "视频时长": "60s",
            "必须出现的内容": "",
            "禁忌或不能出现的内容": "",
        }
        script_rows, detail_rows = generate_template_rows(brief, decisions)

        result = audit_generation(brief, slots, decisions, script_rows, detail_rows)

        self.assertNotIn("invalid_launch_slot_count", result["blocking_items"])
        self.assertNotIn("invalid_launch_slot_mapping", result["blocking_items"])
        self.assertEqual(len(script_rows), 17)
        self.assertEqual(detail_rows[-1]["逻辑槽位"], 18)

    def test_provider_quality_findings_block_automatic_visual_use(self):
        from mvp_v2.image_understanding import analysis_asset_unusable

        blocked_findings = (
            "对焦不准",
            "画质模糊",
            "画面模糊；噪点明显",
            "图像分辨率较低；细节不清晰",
        )
        for finding in blocked_findings:
            with self.subTest(finding=finding):
                self.assertTrue(analysis_asset_unusable({
                    "quality_flags": [],
                    "negative_flags": [finding],
                }))

        self.assertFalse(analysis_asset_unusable({
            "quality_flags": [],
            "negative_flags": ["轻微运动模糊"],
        }))

    def test_cross_brand_creative_text_is_adapted_to_current_project(self):
        text = "林氏家居发布会现场，品牌logo穿越进入主会场"
        adapted = adapt_source_text_to_project(text, "美的智美万象发布会 美的")
        self.assertNotIn("林氏家居", adapted)
        self.assertIn("美的", adapted)
        self.assertIn("logo穿越", adapted)

    def test_replace_uses_cross_brand_creative_function_without_leaking_source_brand(self):
        slot = source_shot(source_sheet="模版", source_row=7, slot_index=1)
        variant = source_shot(
            source_sheet="创意库",
            source_row=8,
            slot_index=1,
            source_brand="林氏家居",
            brand_neutral=False,
            brand_neutral_evidence="",
            visual_desc="林氏家居发布会现场，品牌logo穿越进入主会场",
            note="林氏家居品牌段落",
        )
        decision = TemplateDecision(
            slot=slot,
            variant=variant,
            mode="REPLACE",
            score=90,
            match_reason="镜头功能匹配",
        )

        rows, _ = generate_template_rows({
            "项目名称": "美的智美万象发布会",
            "品牌或客户": "美的",
            "视频时长": "60s",
        }, [decision])

        self.assertNotIn("林氏家居", rows[0]["画面描述"])
        self.assertNotIn("林氏家居", rows[0]["备注"])
        self.assertIn("美的", rows[0]["画面描述"])

    def test_template_generation_assigns_60_second_duration_without_visual_analysis(self):
        decisions = [
            TemplateDecision(
                slot=source_shot(slot_index=index, shot_no=str(index)),
                variant=None,
                mode="KEEP",
                score=0,
                match_reason="保留模板",
            )
            for index in range(1, 19)
        ]

        rows, details = generate_template_rows({"项目名称": "测试发布会", "视频时长": "60s"}, decisions)

        self.assertEqual(len(rows), 18)
        self.assertEqual(sum(row["时长(秒)"] for row in rows), 60)
        self.assertTrue(all("建议时长" in row["备注"] for row in rows))
        self.assertEqual(sum(row["时长(秒)"] for row in details), 60)

    def test_read_briefs_rejects_missing_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brief.xlsx"
            write_wb(path, [["项目名称", "脚本类型"], ["测试项目", "发布会"]])

            with self.assertRaisesRegex(BriefError, "缺少必填字段"):
                read_briefs(path)

    def test_read_briefs_accepts_registered_non_launch_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brief.xlsx"
            write_wb(path, [[
                "项目名称",
                "项目区域文件名",
                "脚本类型",
                "视频时长",
                "风格调性",
                "必须出现的内容",
            ], [
                "测试项目",
                "客户A-年会",
                "年会",
                "60s",
                "科技感",
                "LOGO",
            ]])

            briefs = read_briefs(path)

        self.assertEqual(briefs[0]["脚本类型"], "年会")
        self.assertEqual(briefs[0]["视频时长"], "60s")

    def test_read_briefs_rejects_unregistered_script_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brief.xlsx"
            write_wb(path, [[
                "项目名称",
                "项目区域文件名",
                "脚本类型",
                "视频时长",
                "风格调性",
                "必须出现的内容",
            ], [
                "测试项目",
                "客户A-未知类型",
                "商业片",
                "60s",
                "科技感",
                "LOGO",
            ]])

            with self.assertRaisesRegex(BriefError, "不支持的脚本类型"):
                read_briefs(path)

    def test_fixed_realistic_brief_is_read_as_launch_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixed_real_brief.xlsx"
            write_fixed_real_brief(path)

            briefs = read_briefs(path)

        self.assertEqual(len(briefs), 1)
        self.assertEqual(briefs[0]["项目名称"], "美的智美万象发布会开场短片")
        self.assertEqual(briefs[0]["脚本类型"], "发布会")
        self.assertIn("智美万象主题字样", briefs[0]["必须出现的内容"])

    def test_read_text_brief_parses_fixed_realistic_brief(self):
        brief = read_text_brief(FIXED_REAL_BRIEF_TEXT)

        self.assertEqual(brief["项目名称"], "美的智美万象发布会开场短片")
        self.assertEqual(brief["脚本类型"], "发布会")
        self.assertEqual(brief["项目区域文件名"], "家电-发布会-【美的智美万象发布会】分镜脚本.xlsx")
        self.assertIn("硬核科技感", brief["风格调性"])
        self.assertIn("核心发布仪式", brief["必须出现的内容"])
        self.assertEqual(brief["input_row"], 1)

    def test_brief_profile_normalizes_and_flags_v02_gaps(self):
        profile = brief_profile_from_dict({
            "项目名称": "美的发布会开场短片",
            "项目区域文件名": "美的发布会.xlsx",
            "脚本类型": "发布会",
            "品牌或客户": "美的",
            "视频时长": "60s",
            "风格调性": "科技感",
            "必须出现的内容": "品牌",
            "禁忌或不能出现的内容": "",
            "素材目录或素材文件": "",
            "备注": "开场片",
        })

        self.assertEqual(profile.project_name, "美的发布会开场短片")
        self.assertEqual(profile.duration_seconds, 60)
        self.assertEqual(profile.required_terms, ("品牌",))
        self.assertIn("禁忌字段为空", profile.gap_notes)
        self.assertIn("素材目录为空", profile.gap_notes)
        self.assertIn("必须内容过泛", profile.gap_notes)

    def test_load_launch_source_shots_reads_only_launch_workbooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wb(root / "M+发布会类通用脚本库.xlsx", [
                ["M+发布会类通用脚本库"],
                ["整体说明", "测试"],
                [],
                [],
                ["镜号", "结构", "形式", "内容"],
                [None, None, None, "画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"],
                [1, "开场", "A", None, "闪白", "航拍", "全景", "推进", "发布会主会场开场", "音乐起", ""],
            ], sheet_name="创意库")
            write_wb(root / "M+年会类通用脚本库.xlsx", [
                ["镜号", "结构", "内容"],
                [1, "开场", "年会画面"],
            ])

            shots = load_launch_source_shots(root)

        self.assertEqual({shot.script_type for shot in shots}, {"发布会"})
        self.assertEqual([shot.source_file for shot in shots], ["M+发布会类通用脚本库.xlsx"])

    def test_load_launch_source_shots_inherits_blank_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wb(root / "M+发布会类通用脚本库.xlsx", [
                ["M+发布会类通用脚本库"],
                ["整体说明", "测试"],
                [],
                [],
                ["镜号", "结构", "形式", "内容"],
                [None, None, None, "画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"],
                [1, "开场", "A", None, "闪白", "航拍", "全景", "推进", "发布会主会场开场", "音乐起", ""],
                [2, None, "B", None, "硬切", "地面", "特写", "固定", "品牌 LOGO 特写", "", ""],
            ], sheet_name="创意库")

            shots = load_launch_source_shots(root)

        self.assertEqual([shot.structure for shot in shots], ["开场", "开场"])

    def test_load_template_slots_uses_template_sheet_order_as_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wb(root / "M+发布会类通用脚本库.xlsx", [
                ["M+发布会类通用脚本库"],
                ["整体说明", "测试"],
                [],
                [],
                ["结构", "镜号", "内容"],
                [None, None, "画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注"],
                ["开场造势", 1, None, None, "闪白", "航拍", "全景", "推进", "品牌开场", "音乐起", ""],
                [None, 2, None, None, "硬切", "地面", "特写", "固定", "品牌 LOGO", "", ""],
                ["品牌落版", 3, None, None, "淡出", "地面", "全景", "固定", "品牌落版画面", "音乐收", ""],
            ], sheet_name="模版")

            slots = load_template_slots(root)

        self.assertEqual([slot.slot_index for slot in slots], [1, 2, 3])
        self.assertEqual([slot.structure for slot in slots], ["开场造势", "开场造势", "品牌落版"])
        self.assertTrue(all(slot.source_sheet == "模版" for slot in slots))

    def test_load_creative_variants_uses_creative_sheet_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wb(root / "M+发布会类通用脚本库.xlsx", [
                ["M+发布会类通用脚本库"],
                ["整体说明", "测试"],
                [],
                [],
                ["镜号", "结构", "形式", "内容"],
                [None, None, None, "画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"],
                [1, "落幅", "A", None, "淡出", "地面", "全景", "固定", "品牌口号落版", "音乐收", ""],
            ], sheet_name="创意库")

            variants = load_creative_variants(root)

        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0].source_sheet, "创意库")
        self.assertEqual(variants[0].structure, "落幅")
        self.assertEqual(variants[0].reuse_level, "镜头功能可复用")
        self.assertIn("不得直接复用原品牌文案", variants[0].forbidden_note)
        self.assertEqual(variants[0].missing_metadata, ())

    def test_load_creative_variants_marks_present_but_blank_reuse_cells_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_wb(root / "M+发布会类通用脚本库.xlsx", [
                ["M+发布会类通用脚本库"],
                ["整体说明", "测试"],
                [],
                [],
                ["镜号", "结构", "形式", "画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注", "可复用等级", "禁用说明"],
                [None] * 13,
                [1, "开场造势", "A", None, "淡出", "地面", "全景", "固定", "品牌口号落版", "音乐收", "", "", ""],
            ], sheet_name="创意库")

            variants = load_creative_variants(root)

        self.assertEqual(variants[0].reuse_level, "")
        self.assertIn("可复用等级", variants[0].missing_metadata)
        self.assertIn("禁用说明", variants[0].missing_metadata)

    def test_load_creative_variants_indexes_project_folder_images_by_shot_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media_dir = root / "发布会类" / "美的智美万象发布会"
            media_dir.mkdir(parents=True)
            image_path = media_dir / "02.png"
            image_path.write_bytes(b"fake image")
            write_wb(root / "M+发布会类通用脚本库.xlsx", [
                ["M+发布会类通用脚本库"],
                ["整体说明", "测试"],
                [],
                [],
                ["镜号", "结构", "形式", "内容"],
                [None, None, None, "画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"],
                [2, "开场造势", "B", None, "字体穿梭", "航拍", "全景", "固定", "品牌logo穿梭入画", "", ""],
            ], sheet_name="创意库")

            variants = load_creative_variants(root, project_hint="美的智美万象发布会")

        self.assertEqual(variants[0].reference_image_path, str(image_path))

    def test_normalize_structure_maps_template_and_creative_aliases(self):
        self.assertEqual(normalize_structure("核心宣讲\n与仪式"), "核心宣讲与仪式")
        self.assertEqual(normalize_structure("核心宣讲"), "核心宣讲与仪式")
        self.assertEqual(normalize_structure("人气聚集"), "人气与现场互动")
        self.assertEqual(normalize_structure("落幅"), "品牌落版")

    def test_all_launch_slots_have_v02_structured_knowledge(self):
        self.assertEqual(set(LAUNCH_SLOT_LOGIC), set(range(1, 19)))

        for index in range(1, 19):
            logic = slot_logic(index)
            self.assertTrue(logic.allowed_materials, index)
            self.assertTrue(logic.blocked_materials, index)
            self.assertTrue(logic.recommended_shot_sizes, index)
            self.assertTrue(logic.recommended_camera_moves, index)
            self.assertTrue(logic.rhythm, index)
            self.assertTrue(logic.required_visual_evidence, index)
            self.assertTrue(logic.repeat_policy, index)

    def test_match_template_slots_replaces_with_same_normalized_creative_variant(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=9,
            structure="品牌落版",
            visual_desc="模板品牌落版",
            reference_image_path="",
        )
        variant = source_shot(
            source_sheet="创意库",
            source_row=30,
            structure="落幅",
            visual_desc="品牌 LOGO 与口号落版，硬核科技感收束",
            effect="淡出",
        )

        decisions = match_template_slots({
            "脚本类型": "发布会",
            "风格调性": "硬核科技感",
            "必须出现的内容": "品牌 LOGO、口号",
            "禁忌或不能出现的内容": "竞品",
        }, [slot], [variant])

        self.assertEqual(decisions[0].mode, "REPLACE")
        self.assertEqual(decisions[0].slot.source_sheet, "模版")
        self.assertEqual(decisions[0].variant.source_sheet, "创意库")
        self.assertGreaterEqual(decisions[0].score, 70)

    def test_match_template_slots_keeps_template_when_no_good_variant(self):
        slot = source_shot(source_sheet="模版", structure="人气与现场互动", visual_desc="模板互动")
        variant = source_shot(source_sheet="创意库", structure="开场造势", visual_desc="品牌 LOGO 开场")

        decisions = match_template_slots({
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "LOGO",
            "禁忌或不能出现的内容": "竞品",
        }, [slot], [variant])

        self.assertEqual(decisions[0].mode, "KEEP")
        self.assertIsNone(decisions[0].variant)
        self.assertIn("无同结构创意库候选", decisions[0].gap_note)

    def test_match_template_slots_prefers_distinct_creative_variants_when_available(self):
        slots = [
            source_shot(source_sheet="模版", source_row=7, slot_index=1, structure="人气与现场互动", visual_desc="模板互动 1"),
            source_shot(source_sheet="模版", source_row=8, slot_index=2, structure="人气与现场互动", visual_desc="模板互动 2"),
        ]
        variants = [
            source_shot(source_sheet="创意库", source_row=30, structure="人气聚集", visual_desc="品牌 LOGO 互动打卡，硬核科技感"),
            source_shot(source_sheet="创意库", source_row=31, structure="人气聚集", visual_desc="活动主会场人流互动，硬核科技感"),
        ]

        decisions = match_template_slots({
            "脚本类型": "发布会",
            "风格调性": "硬核科技感",
            "必须出现的内容": "品牌 LOGO、活动主会场、人流互动",
            "禁忌或不能出现的内容": "竞品",
        }, slots, variants)

        used_rows = [decision.variant.source_row for decision in decisions if decision.variant]
        self.assertEqual(len(used_rows), len(set(used_rows)))

    def test_score_candidates_excludes_forbidden_and_disabled_shots(self):
        brief = {
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "LOGO",
            "禁忌或不能出现的内容": "竞品",
        }
        shots = [
            source_shot(visual_desc="竞品 LOGO 出现在主会场"),
            source_shot(source_row=8, reuse_level="禁用", visual_desc="品牌 LOGO 主会场"),
            source_shot(source_row=9, visual_desc="品牌 LOGO 主会场，科技感灯光"),
        ]

        scored = score_candidates(brief, shots)

        self.assertEqual([item.shot.source_row for item in scored], [9])

    def test_score_candidates_warns_when_reuse_metadata_is_missing(self):
        scored = score_candidates({
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "LOGO",
            "禁忌或不能出现的内容": "",
        }, [source_shot()])

        self.assertTrue(scored)
        self.assertIn("缺少可复用等级", scored[0].gap_note)
        self.assertIn("禁忌字段为空", scored[0].gap_note)

    def test_score_candidates_orders_by_score_not_input_order(self):
        brief = {
            "脚本类型": "发布会",
            "风格调性": "硬核科技感",
            "必须出现的内容": "LOGO、活动主会场",
            "禁忌或不能出现的内容": "竞品",
        }
        weak = source_shot(source_row=7, visual_desc="普通观众入场", effect="", reference_image_path="")
        strong = source_shot(source_row=8, visual_desc="品牌 LOGO 穿梭进入活动主会场，硬核科技感", effect="字体穿梭")

        scored = score_candidates(brief, [weak, strong])

        self.assertEqual(scored[0].shot.source_row, 8)
        self.assertGreater(scored[0].score, scored[1].score)
        self.assertIn("匹配分", scored[0].match_reason)

    def test_generate_script_rows_produces_reviewable_rows_with_gap_notes(self):
        brief = {
            "项目名称": "测试发布会",
            "input_row": 2,
            "视频时长": "60s",
        }
        scored = score_candidates({
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "LOGO",
            "禁忌或不能出现的内容": "",
        }, [source_shot()])

        rows = generate_script_rows(brief, scored)

        self.assertEqual(rows[0]["项目名称"], "测试发布会")
        self.assertIn("匹配分", rows[0])
        self.assertIn("缺口说明", rows[0])

    def test_generate_template_rows_preserves_template_order_and_detail_sources(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=7,
            shot_no="1",
            structure="开场造势",
            visual_desc="模板开场",
        )
        variant = source_shot(
            source_sheet="创意库",
            source_row=8,
            structure="开场造势",
            visual_desc="品牌 LOGO 科技感开场",
        )
        decisions = match_template_slots({
            "项目名称": "测试发布会",
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "LOGO",
            "禁忌或不能出现的内容": "竞品",
        }, [slot], [variant])

        script_rows, detail_rows = generate_template_rows({
            "项目名称": "测试发布会", "input_row": 2, "视频时长": "60s",
        }, decisions)

        self.assertEqual(script_rows[0]["镜号"], "1")
        self.assertEqual(script_rows[0]["结构"], "开场造势")
        self.assertEqual(script_rows[0]["画面描述"], "品牌 LOGO 科技感开场")
        self.assertEqual(detail_rows[0]["模板来源 sheet"], "模版")
        self.assertEqual(detail_rows[0]["替换来源 sheet"], "创意库")
        self.assertEqual(detail_rows[0]["替换方式"], "REPLACE")
        self.assertIn("允许素材", detail_rows[0])
        self.assertIn("必须视觉证据", detail_rows[0])
        self.assertIn("候选命中点", detail_rows[0])
        self.assertIn("候选缺失点", detail_rows[0])
        self.assertIn("候选冲突点", detail_rows[0])

    def test_generate_template_rows_writes_brief_profile_gaps_to_detail(self):
        slot = source_shot(source_sheet="模版", source_row=7, slot_index=1)
        decisions = match_template_slots({
            "项目名称": "测试发布会",
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "品牌",
            "禁忌或不能出现的内容": "",
            "素材目录或素材文件": "",
        }, [slot], [])

        _, detail_rows = generate_template_rows({
            "项目名称": "测试发布会",
            "input_row": 1,
            "脚本类型": "发布会",
            "视频时长": "60s",
            "风格调性": "科技感",
            "必须出现的内容": "品牌",
            "禁忌或不能出现的内容": "",
            "素材目录或素材文件": "",
        }, decisions)

        self.assertIn("禁忌字段为空", detail_rows[0]["缺口说明"])
        self.assertIn("素材目录为空", detail_rows[0]["缺口说明"])
        self.assertIn("必须内容过泛", detail_rows[0]["缺口说明"])

    def test_generate_template_rows_plans_required_terms_and_keeps_evidence_pending(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=13,
            shot_no="7",
            slot_index=7,
            visual_desc="来宾体验家居主题空间",
        )
        brief = {
            "项目名称": "林氏家居生活方式发布会",
            "品牌或客户": "林氏家居",
            "脚本类型": "发布会",
            "脚本类型": "发布会",
            "视频时长": "60s",
            "风格调性": "潮酷",
            "必须出现的内容": "主题装置、产品体验",
            "禁忌或不能出现的内容": "其他品牌",
            "素材目录或素材文件": "林氏家居",
        }
        decisions = match_template_slots(brief, [slot], [])

        script_rows, detail_rows = generate_template_rows(brief, decisions)

        self.assertIn("主题装置", script_rows[0]["画面描述"])
        self.assertIn("产品体验", script_rows[0]["画面描述"])
        self.assertNotIn("镜头明确呈现", script_rows[0]["画面描述"])
        self.assertIn("Brief规划内容", detail_rows[0]["缺口说明"])
        self.assertIn("主题装置", detail_rows[0]["缺口说明"])
        self.assertIn("产品体验", detail_rows[0]["缺口说明"])

    def test_visual_rewrite_never_replaces_existing_brief_copy_with_caption(self):
        brief = {
            "项目名称": "林氏家居生活方式发布会开场短片",
            "品牌或客户": "林氏家居",
            "视频时长": "60s",
            "必须出现的内容": "品牌logo、核心发布仪式",
        }
        script_rows = [{"画面描述": f"镜头{index}"} for index in range(1, 19)]
        detail_rows = [{"slot_index": index} for index in range(1, 19)]
        visual_assets = [{
            "slot_index": 18,
            "图片角色": "画面示意",
            "配图判断": "匹配",
            "视觉内容摘要": "林氏家居发布会主视觉KV",
            "原图路径": "/tmp/kv.png",
            "image_id": "img_kv",
        }]

        rewritten, _, _ = rewrite_template_rows_from_visual_evidence(
            brief,
            script_rows,
            detail_rows,
            visual_assets,
        )

        self.assertEqual(rewritten[17]["画面描述"], "镜头18")
        self.assertNotIn("智美万象", rewritten[17]["画面描述"])

    def test_generate_template_rows_carries_creative_image_anchors_without_debug_text(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=7,
            shot_no="1",
            structure="开场造势",
            visual_desc="模板开场",
            reference_image_path="template.xlsx#模版!R7C3",
        )
        variant = source_shot(
            source_sheet="创意库",
            source_row=8,
            structure="开场造势",
            visual_desc="品牌 LOGO 科技感开场",
            reference_image_path="launch.xlsx#创意库!R8C4",
            effect_reference_path="launch.xlsx#创意库!R8C5",
        )
        decisions = match_template_slots({
            "项目名称": "测试发布会",
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "LOGO",
            "禁忌或不能出现的内容": "竞品",
        }, [slot], [variant])

        script_rows, detail_rows = generate_template_rows({
            "项目名称": "测试发布会", "input_row": 2, "视频时长": "60s",
        }, decisions)

        self.assertEqual(script_rows[0]["画面示意"], "launch.xlsx#创意库!R8C4")
        self.assertEqual(script_rows[0]["效果示意"], "launch.xlsx#创意库!R8C5")
        self.assertNotIn("承接模板", script_rows[0]["画面描述"])
        self.assertEqual(detail_rows[0]["输出图片来源"], "创意库")

    def test_generate_template_rows_falls_back_to_template_image_when_variant_has_no_image(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=7,
            shot_no="1",
            structure="开场造势",
            visual_desc="品牌logo穿梭入画",
            reference_image_path="template.xlsx#模版!R7C3",
        )
        variant = source_shot(
            source_sheet="创意库",
            source_row=8,
            structure="开场造势",
            visual_desc="品牌logo穿梭入画",
            reference_image_path="",
            effect_reference_path="",
        )
        decisions = match_template_slots({
            "项目名称": "测试发布会",
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "logo",
            "禁忌或不能出现的内容": "",
        }, [slot], [variant])

        script_rows, detail_rows = generate_template_rows({
            "项目名称": "测试发布会", "input_row": 1, "视频时长": "60s",
        }, decisions)

        self.assertEqual(script_rows[0]["画面示意"], "template.xlsx#模版!R7C3")
        self.assertEqual(detail_rows[0]["输出图片来源"], "模版")

    def test_conflicting_creative_image_alone_cannot_trigger_adapt(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=10,
            shot_no="4",
            structure="核心宣讲与仪式",
            visual_desc="主会场空镜，多束白色光束向中心汇聚",
            effect="",
            camera_type="地面",
            shot_size="全景",
            camera_move="缓缓前推",
            sound="环境声",
            note="模板备注",
            reference_image_path="",
        )
        variant = source_shot(
            source_sheet="创意库",
            source_row=41,
            structure="核心宣讲",
            visual_desc="主持人报幕欢迎，结合活动主题",
            effect="画中画",
            camera_type="地面",
            shot_size="中景",
            camera_move="推近",
            sound="掌声",
            note="开场",
            reference_image_path="launch.xlsx#创意库!R41C4",
        )

        decisions = match_template_slots({
            "项目名称": "测试发布会",
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "LOGO",
            "禁忌或不能出现的内容": "竞品",
        }, [slot], [variant])
        script_rows, detail_rows = generate_template_rows({
            "项目名称": "测试发布会", "input_row": 1, "视频时长": "60s",
        }, decisions)

        self.assertEqual(decisions[0].mode, "KEEP")
        self.assertEqual(script_rows[0]["画面描述"], "主会场空镜，多束白色光束向中心汇聚")
        self.assertEqual(script_rows[0]["类型"], "地面")
        self.assertEqual(script_rows[0]["景别"], "全景")
        self.assertEqual(script_rows[0]["运镜"], "缓缓前推")
        self.assertEqual(script_rows[0]["声音"], "环境声")
        self.assertIn("模板备注", script_rows[0]["备注"])
        self.assertIn("建议时长：60s", script_rows[0]["备注"])
        self.assertNotEqual(script_rows[0]["效果"], "画中画")
        self.assertEqual(script_rows[0]["画面示意"], "")
        self.assertEqual(detail_rows[0]["替换方式"], "KEEP")
        self.assertIn("镜头职责不足", detail_rows[0]["候选选择理由"])

    def test_slot_visual_match_beats_global_brief_keyword_match(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=7,
            slot_index=1,
            structure="开场造势",
            visual_desc="品牌logo穿梭入画",
            effect="字体穿梭",
            shot_size="全景",
            camera_move="航拍固定",
        )
        global_hit = source_shot(
            source_row=7,
            structure="开场造势",
            visual_desc="版权素材：从地标空镜转场至活动主会场外观",
            effect="AI首尾帧",
            shot_size="远景",
            camera_move="航拍推进",
            reference_image_path="launch.xlsx#创意库!R7C4",
        )
        slot_hit = source_shot(
            source_row=8,
            structure="开场造势",
            visual_desc="品牌logo穿梭入画",
            effect="字体穿梭",
            shot_size="全景",
            camera_move="航拍固定",
            reference_image_path="",
        )

        decisions = match_template_slots({
            "项目名称": "测试发布会",
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "活动主会场",
            "禁忌或不能出现的内容": "",
        }, [slot], [global_hit, slot_hit])

        self.assertEqual(decisions[0].variant.source_row, 8)
        self.assertIn("槽位画面匹配", decisions[0].match_reason)

    def test_empty_template_effect_does_not_count_as_effect_match(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=10,
            structure="核心宣讲与仪式",
            visual_desc="主会场空镜",
            effect="",
        )
        variant = source_shot(
            source_row=41,
            structure="核心宣讲",
            visual_desc="主持人报幕欢迎，结合活动主题",
            effect="画中画",
        )

        decisions = match_template_slots({
            "项目名称": "测试发布会",
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "活动主会场",
            "禁忌或不能出现的内容": "",
        }, [slot], [variant])

        self.assertNotIn("效果匹配", decisions[0].match_reason)

    def test_repeated_template_transition_can_reuse_exact_creative_variant(self):
        slot_a = source_shot(
            source_sheet="模版",
            source_row=9,
            slot_index=3,
            structure="核心宣讲与仪式",
            visual_desc='品牌logo 慢门抽帧素材快闪后 浮现穿越机用 "贴屏变速飞行" 制造视觉冲击力',
            camera_type="穿越机",
            shot_size="广角",
            camera_move="穿越机俯冲",
        )
        slot_b = source_shot(
            source_sheet="模版",
            source_row=16,
            slot_index=10,
            structure="核心宣讲与仪式",
            visual_desc='品牌logo 慢门抽帧素材快闪后 浮现穿越机用 "贴屏变速飞行" 制造视觉冲击力',
            camera_type="穿越机",
            shot_size="广角",
            camera_move="穿越机俯冲",
        )
        exact = source_shot(
            source_row=39,
            structure="核心宣讲与仪式",
            visual_desc='品牌logo 慢门抽帧素材快闪后 浮现穿越机用 "贴屏变速飞行" 制造视觉冲击力',
            camera_type="穿越机",
            shot_size="广角",
            camera_move="穿越机俯冲",
        )
        weak = source_shot(
            source_row=46,
            structure="核心宣讲",
            visual_desc="常规中场表演",
            effect="闪光灯",
        )

        decisions = match_template_slots({
            "项目名称": "测试发布会",
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "核心发布仪式",
            "禁忌或不能出现的内容": "",
        }, [slot_a, slot_b], [exact, weak])

        self.assertEqual([decision.variant.source_row for decision in decisions], [39, 39])

    def test_brand_transition_slot_rejects_performance_creative(self):
        slot = source_shot(
            source_sheet="模版", source_row=9, slot_index=3,
            structure="核心宣讲与仪式", visual_desc="品牌logo视觉转场",
        )
        performance = source_shot(
            source_row=46, structure="核心宣讲与仪式",
            visual_desc="常规中场表演", source_brand="测试品牌",
        )

        decisions = match_template_slots({
            "项目名称": "测试品牌发布会", "品牌或客户": "测试品牌",
            "脚本类型": "发布会", "视频时长": "60s", "风格调性": "潮酷",
            "必须出现的内容": "品牌logo", "禁忌或不能出现的内容": "",
        }, [slot], [performance])

        self.assertEqual(decisions[0].mode, "KEEP")

    def test_brand_transition_slot_rejects_speech_creative(self):
        slot = source_shot(
            source_sheet="模版", source_row=9, slot_index=3,
            structure="核心宣讲与仪式", visual_desc="品牌logo视觉转场",
        )
        speech = source_shot(
            source_row=43, structure="核心宣讲与仪式",
            visual_desc="演讲者站在大屏前发布核心战略口号",
            source_brand="测试品牌",
        )

        decisions = match_template_slots({
            "项目名称": "测试品牌发布会", "品牌或客户": "测试品牌",
            "脚本类型": "发布会", "视频时长": "60s", "风格调性": "科技感",
            "必须出现的内容": "品牌logo", "禁忌或不能出现的内容": "",
        }, [slot], [speech])

        self.assertEqual(decisions[0].mode, "KEEP")

    def test_template_source_has_explicit_reuse_policy(self):
        policy = source_reuse_policy("M+发布会类通用脚本库.xlsx", "模版")

        self.assertEqual(policy["reuse_level"], "可复用")

    def test_versioned_template_source_keeps_reuse_policy(self):
        policy = source_reuse_policy(
            "M+发布会类通用脚本库 AI版v20260720.xlsx",
            "模版",
        )

        self.assertEqual(policy["reuse_level"], "可复用")

    def test_keep_decision_is_visible_but_not_a_quality_warning(self):
        slot = source_shot(source_sheet="模版", source_row=7, slot_index=1)
        decision = TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板")
        script_rows, detail_rows = generate_template_rows(
            {"项目名称": "测试发布会", "视频时长": "60s"}, [decision]
        )

        result = audit_generation(
            {"项目名称": "测试发布会", "视频时长": "60s"},
            [slot], [decision], script_rows, detail_rows,
        )

        self.assertNotIn("template_keep", {check["检查项"] for check in result["checks"]})

    def test_low_score_adapt_without_image_keeps_template_and_does_not_consume_variant(self):
        weak_slot = source_shot(
            source_sheet="模版",
            source_row=10,
            slot_index=4,
            structure="核心宣讲与仪式",
            visual_desc="主会场空镜，多束白色光束向中心汇聚",
        )
        later_slot = source_shot(
            source_sheet="模版",
            source_row=19,
            slot_index=13,
            structure="核心宣讲与仪式",
            visual_desc="观众举起手机录屏画面",
            shot_size="特写",
            camera_move="横移",
        )
        phone = source_shot(
            source_row=45,
            structure="核心宣讲",
            visual_desc="观众举起手机，通过屏幕录制舞台上的发布会",
            shot_size="特写",
            camera_move="横移",
            reference_image_path="",
            effect_reference_path="",
        )

        decisions = match_template_slots({
            "项目名称": "测试发布会",
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "活动主会场",
            "禁忌或不能出现的内容": "",
        }, [weak_slot, later_slot], [phone])

        self.assertEqual(decisions[0].mode, "KEEP")
        self.assertEqual(decisions[1].variant.source_row, 45)

    def test_theme_space_slot_rejects_signin_candidate(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=13,
            slot_index=7,
            structure="人气与现场互动",
            visual_desc="客户在主题空间互动",
        )
        signin = source_shot(
            source_row=32,
            structure="人气与现场互动",
            visual_desc="签到墙运用人物抠像效果快切3-4位顾客摆拍",
            effect="人物抠像",
        )
        experience = source_shot(
            source_row=37,
            structure="人气与现场互动",
            visual_desc="产品互动体验",
        )

        decisions = match_template_slots({
            "项目名称": "测试发布会",
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "人流互动",
            "禁忌或不能出现的内容": "",
        }, [slot], [signin, experience])

        self.assertEqual(decisions[0].variant.source_row, 37)

    def test_main_venue_establishing_slot_can_use_installation_empty_shot(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=10,
            slot_index=4,
            structure="核心宣讲与仪式",
            visual_desc="主会场空镜，多束白色光束向中心汇聚主会场",
            shot_size="全景",
        )
        empty_shot = source_shot(
            source_row=27,
            structure="装置空镜",
            visual_desc="主会场空镜，多束白色光束向中心汇聚主会场",
            shot_size="全景",
            reference_image_path="/tmp/21.png",
        )
        generic_stage = source_shot(
            source_row=42,
            structure="核心宣讲",
            visual_desc="主发布会现场，台下座无虚席，大屏显示本次活动主题",
            shot_size="全景",
            reference_image_path="/tmp/36.png",
        )

        decisions = match_template_slots({
            "项目名称": "测试发布会",
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "活动主会场",
            "禁忌或不能出现的内容": "",
        }, [slot], [empty_shot, generic_stage])

        self.assertEqual(decisions[0].variant.source_row, 27)

    def test_read_text_brief_accepts_freeform_paragraph(self):
        text = (
            "请为美的智美万象发布会做一支60秒开场短片，脚本类型是发布会。"
            "整体要硬核科技感、冷峻纯净、高级平稳大气。"
            "必须出现美的品牌logo、活动主会场、智美万象主题字样、装置空镜、人流互动和核心发布仪式。"
            "不要低质感口号堆砌，不要泛泛写品牌宣传片，不要脱离分镜字段。"
        )

        brief = read_text_brief(text)

        self.assertEqual(brief["项目名称"], "美的智美万象发布会开场短片")
        self.assertEqual(brief["脚本类型"], "发布会")
        self.assertEqual(brief["视频时长"], "60s")
        self.assertIn("硬核科技感", brief["风格调性"])
        self.assertIn("智美万象主题字样", brief["必须出现的内容"])
        self.assertIn("不要低质感口号堆砌", brief["禁忌或不能出现的内容"])

    def test_generate_template_rows_polishes_generic_brand_and_theme_from_text_brief(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=7,
            slot_index=1,
            shot_no="1",
            structure="开场造势",
            visual_desc="品牌logo穿梭入画，浮现活动主题文字",
        )
        decisions = match_template_slots(read_text_brief(FIXED_REAL_BRIEF_TEXT), [slot], [])

        script_rows, detail_rows = generate_template_rows(read_text_brief(FIXED_REAL_BRIEF_TEXT), decisions)

        self.assertIn("美的品牌logo", script_rows[0]["画面描述"])
        self.assertIn("智美万象主题字样", script_rows[0]["画面描述"])
        self.assertEqual(detail_rows[0]["槽位功能"], "品牌开场识别")
        self.assertIn("品牌 logo", detail_rows[0]["槽位选择原因"])

    def test_write_output_workbook_has_fixed_columns_and_reopens(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.xlsx"
            rows = [{
                "项目名称": "测试发布会",
                "需求行号": 2,
                "镜号": "001",
                "结构": "开场",
                "时长(秒)": 3,
                "画面示意": "",
                "效果示意": "",
                "效果": "闪白",
                "景别": "全景",
                "运镜": "推进",
                "画面描述": "品牌 LOGO 入画",
                "声音": "音乐起",
                "备注": "",
                "参考图路径": "",
                "效果示意路径": "",
                "来源文件": "launch.xlsx",
                "来源 sheet": "创意库",
                "来源行": 7,
                "匹配分": 90,
                "匹配理由": "匹配分 90",
                "缺口说明": "",
                "审核结果": "",
                "修改意见": "",
            }]

            write_output_workbook(path, rows)
            wb = load_workbook(path)
            ws = wb["脚本"]

            self.assertEqual([cell.value for cell in ws[1]], OUTPUT_COLUMNS)
            self.assertEqual(ws["A2"].value, "测试发布会")
            self.assertEqual(ws["S2"].value, 90)
            wb.close()

    def test_write_template_output_workbook_has_script_and_detail_sheets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "template_output.xlsx"
            script_rows = [{
                "结构": "开场造势",
                "镜号": "1",
                "画面示意": "",
                "效果示意": "",
                "效果": "闪白",
                "类型": "航拍",
                "景别": "全景",
                "运镜": "推进",
                "画面描述": "品牌开场",
                "声音": "音乐起",
                "备注": "",
            }]
            detail_rows = [{
                "项目名称": "测试发布会",
                "需求行号": 2,
                "slot_index": 1,
                "替换方式": "KEEP",
                "匹配分": 0,
            }]

            write_template_output_workbook(path, script_rows, detail_rows)
            wb = load_workbook(path, read_only=True)

            self.assertEqual(
                wb.sheetnames,
                ["脚本", "抽调明细", "剪辑复核", "相邻镜头复核", "整片复核"],
            )
            self.assertEqual([cell.value for cell in wb["脚本"][1]], SCRIPT_COLUMNS)
            self.assertEqual([cell.value for cell in wb["抽调明细"][1]], DETAIL_COLUMNS)
            wb.close()

    def test_write_template_output_workbook_can_use_original_template_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "shot.png"
            Image.new("RGB", (320, 180), color="white").save(image_path)
            path = Path(tmp) / "template_output.xlsx"
            script_rows = [{
                "结构": "开场造势",
                "镜号": "1",
                "画面示意": str(image_path),
                "效果示意": "",
                "效果": "字体穿梭",
                "类型": "航拍",
                "景别": "全景",
                "运镜": "推进",
                "画面描述": "品牌logo穿梭入画",
                "声音": "音乐起",
                "备注": "",
            }]

            write_template_output_workbook(
                path,
                script_rows,
                [],
                [],
                template_workbook_path=anonymous_launch_template(Path(tmp) / "source"),
            )
            wb = load_workbook(path)
            ws = wb["脚本"]

            self.assertIn("A1:K1", [str(item) for item in ws.merged_cells.ranges])
            self.assertIn("C5:K5", [str(item) for item in ws.merged_cells.ranges])
            self.assertEqual(ws["A1"].value, "M+发布会类通用模版")
            self.assertEqual(ws["A5"].value, "结构")
            self.assertEqual(ws["C6"].value, "画面示意")
            self.assertEqual(ws["A7"].value, "开场造势")
            self.assertEqual(ws["B7"].value, "1")
            self.assertIsNone(ws["C7"].value)
            self.assertGreaterEqual(ws.row_dimensions[7].height, 120)
            self.assertGreater(ws.column_dimensions["C"].width, 80)
            self.assertGreater(ws.column_dimensions["I"].width, 60)
            self.assertEqual(len(ws._images), 1)
            wb.close()

    def test_template_output_embeds_original_image_pixels_not_low_res_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "shot.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            output = Path(tmp) / "template_output.xlsx"

            write_template_output_workbook(output, [{
                "结构": "开场造势",
                "镜号": "1",
                "画面示意": str(image_path),
                "效果示意": "",
                "效果": "",
                "类型": "",
                "景别": "",
                "运镜": "",
                "画面描述": "品牌logo穿梭入画",
                "声音": "",
                "备注": "",
            }], [], [], template_workbook_path=anonymous_launch_template(Path(tmp) / "source"))

            with zipfile.ZipFile(output) as archive, tempfile.TemporaryDirectory() as media_tmp:
                media_name = [name for name in archive.namelist() if name.startswith("xl/media/")][0]
                extracted = Path(media_tmp) / Path(media_name).name
                extracted.write_bytes(archive.read(media_name))
                with Image.open(extracted) as embedded:
                    self.assertEqual(embedded.size, (1920, 1080))

    def test_attach_visual_assets_reads_original_dimensions_and_enriches_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "01.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            script_rows = [{
                "结构": "开场造势",
                "镜号": "1",
                "画面示意": str(image_path),
                "效果示意": "",
                "画面描述": "品牌logo穿梭入画",
            }]
            detail_rows = [{
                "slot_index": 1,
                "模板镜号": "1",
                "替换方式": "REPLACE",
                "替换来源文件": "M+发布会类通用脚本库.xlsx",
                "替换来源 sheet": "创意库",
                "替换来源行": 8,
                "匹配理由": "匹配分 100；槽位画面匹配:品牌、logo",
                "输出图片来源": "创意库",
            }]

            enriched_details, visual_assets = attach_visual_assets(script_rows, detail_rows)

        self.assertEqual(len(visual_assets), 1)
        self.assertRegex(visual_assets[0]["visual_asset_id"], r"^va_[0-9a-f]{12}$")
        self.assertEqual(visual_assets[0]["原图宽"], 1920)
        self.assertEqual(visual_assets[0]["原图高"], 1080)
        self.assertEqual(visual_assets[0]["图片角色"], "画面示意")
        self.assertEqual(visual_assets[0]["绑定方式"], "行绑定")
        self.assertIn("抽调行对应图片", visual_assets[0]["绑定说明"])
        self.assertEqual(enriched_details[0]["视觉资产ID"], visual_assets[0]["visual_asset_id"])
        self.assertEqual(enriched_details[0]["图片绑定方式"], "行绑定")

    def test_numbered_project_image_fallback_is_marked_for_visual_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "02.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            script_rows = [{
                "结构": "开场造势",
                "镜号": "1",
                "画面示意": str(image_path),
                "效果示意": "",
                "画面描述": "品牌logo穿梭入画",
            }]
            detail_rows = [{
                "slot_index": 1,
                "模板镜号": "1",
                "替换方式": "REPLACE",
                "替换来源文件": "M+发布会类通用脚本库.xlsx",
                "替换来源 sheet": "创意库",
                "替换来源行": 8,
                "匹配理由": "匹配分 100；槽位画面匹配:品牌、logo",
                "输出图片来源": "创意库",
                "图片证据类型": "项目编号回退",
            }]

            enriched_details, visual_assets = attach_visual_assets(script_rows, detail_rows)

        self.assertEqual(visual_assets[0]["绑定方式"], "编号回退")
        self.assertEqual(visual_assets[0]["视觉读取状态"], "未读取")
        self.assertIn("需视觉复核", visual_assets[0]["绑定说明"])
        self.assertEqual(enriched_details[0]["配图置信度"], "低")

    def test_apply_visual_analysis_scores_image_against_script_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "02.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            script_rows = [{
                "镜号": "1",
                "画面示意": str(image_path),
                "画面描述": "美的品牌logo穿梭入画",
            }]
            detail_rows = [{
                "slot_index": 1,
                "模板镜号": "1",
                "替换方式": "REPLACE",
                "替换来源文件": "M+发布会类通用脚本库.xlsx",
                "替换来源 sheet": "创意库",
                "替换来源行": 8,
                "图片证据类型": "项目编号回退",
                "匹配理由": "匹配分 100；槽位画面匹配:品牌、logo",
                "输出图片来源": "创意库",
            }]
            enriched_details, visual_assets = attach_visual_assets(script_rows, detail_rows)
            analysis = {
                "02.png": {
                    "summary": "蓝色外场建筑与 Midea 门头",
                    "tags": ["外场", "建筑", "Midea门头", "主会场外观"],
                    "source": "人工复核",
                }
            }

            enriched_details, visual_assets = apply_visual_analysis(
                script_rows,
                enriched_details,
                visual_assets,
                analysis,
            )

        self.assertEqual(visual_assets[0]["视觉读取状态"], "人工复核")
        self.assertEqual(visual_assets[0]["视觉内容摘要"], "蓝色外场建筑与 Midea 门头")
        self.assertIn("外场", visual_assets[0]["视觉标签"])
        self.assertLess(visual_assets[0]["视觉匹配分"], 70)
        self.assertIn("logo/品牌标识", visual_assets[0]["缺失视觉要素"])
        self.assertEqual(visual_assets[0]["配图判断"], "不匹配")
        self.assertEqual(enriched_details[0]["配图置信度"], "低")
        self.assertIn("不匹配", enriched_details[0]["图片绑定说明"])

    def test_visual_analysis_does_not_count_negated_terms_as_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "stage.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            script_rows = [{
                "镜号": "2",
                "画面示意": str(image_path),
                "画面描述": "搭建延时：场馆从空旷到搭建完成的缩影",
            }]
            detail_rows = [{
                "slot_index": 2,
                "模板镜号": "2",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 13,
                "匹配理由": "匹配分 90",
                "输出图片来源": "创意库",
            }]
            enriched_details, visual_assets = attach_visual_assets(script_rows, detail_rows)
            analysis = {
                "stage.png": {
                    "summary": "暗场舞台灯光和大屏，不是搭建延时，也不是空场施工",
                    "tags": ["舞台", "灯光", "大屏"],
                    "source": "人工复核",
                }
            }

            enriched_details, visual_assets = apply_visual_analysis(
                script_rows,
                enriched_details,
                visual_assets,
                analysis,
            )

        self.assertEqual(visual_assets[0]["配图判断"], "不匹配")
        self.assertIn("搭建/施工延时", visual_assets[0]["缺失视觉要素"])

    def test_visual_analysis_does_not_count_unclear_logo_as_logo_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "stage.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            script_rows = [{
                "镜号": "10",
                "画面示意": str(image_path),
                "画面描述": "美的品牌logo 慢门抽帧素材快闪",
            }]
            detail_rows = [{
                "slot_index": 10,
                "模板镜号": "10",
                "项目名称": "美的智美万象发布会开场短片",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 39,
                "匹配理由": "匹配分 100",
                "输出图片来源": "创意库",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            analysis = {
                "stage.png": {
                    "summary": "暗场舞台与大屏，人影走动，品牌 logo 不明确",
                    "tags": ["舞台", "大屏", "暗场", "人物"],
                    "source": "人工复核",
                }
            }

            detail_rows, visual_assets = apply_visual_analysis(script_rows, detail_rows, visual_assets, analysis)

        self.assertEqual(visual_assets[0]["配图判断"], "不匹配")
        self.assertIn("logo/品牌标识", visual_assets[0]["缺失视觉要素"])

    def test_visual_analysis_rejects_blurry_logo_as_clear_logo_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "blur_logo.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            script_rows = [{
                "镜号": "1",
                "画面示意": str(image_path),
                "画面描述": "美的品牌logo穿梭入画",
            }]
            detail_rows = [{
                "slot_index": 1,
                "模板镜号": "1",
                "项目名称": "美的智美万象发布会开场短片",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 8,
                "匹配理由": "匹配分 100",
                "输出图片来源": "创意库",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            analysis = {
                "blur_logo.png": {
                    "summary": "Midea 标识虚焦特写，不适合作为清晰 logo 证据",
                    "tags": ["Midea", "logo", "虚焦", "低清晰标识"],
                    "source": "人工复核",
                }
            }

            detail_rows, visual_assets = apply_visual_analysis(script_rows, detail_rows, visual_assets, analysis)

        self.assertEqual(visual_assets[0]["配图判断"], "不匹配")
        self.assertIn("清晰 logo", visual_assets[0]["冲突视觉要素"])

    def test_visual_analysis_rejects_empty_exhibit_as_build_timelapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "empty_exhibit.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            script_rows = [{
                "镜号": "2",
                "画面示意": str(image_path),
                "画面描述": "搭建延时：场馆从空旷到搭建完成的缩影",
            }]
            detail_rows = [{
                "slot_index": 2,
                "模板镜号": "2",
                "项目名称": "美的智美万象发布会开场短片",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 13,
                "匹配理由": "匹配分 100",
                "输出图片来源": "创意库",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            analysis = {
                "empty_exhibit.png": {
                    "summary": "COLMO 展区汽车与顶部圆形装置，空间空旷，偏汽车展区空镜",
                    "tags": ["COLMO", "汽车", "展区", "空镜", "环形装置"],
                    "source": "人工复核",
                }
            }

            detail_rows, visual_assets = apply_visual_analysis(script_rows, detail_rows, visual_assets, analysis)

        self.assertEqual(visual_assets[0]["配图判断"], "不匹配")
        self.assertIn("搭建/施工延时", visual_assets[0]["缺失视觉要素"])

    def test_visual_analysis_rejects_exterior_as_main_stage_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "exterior.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            script_rows = [{
                "镜号": "4",
                "画面示意": str(image_path),
                "画面描述": "主会场空镜，多束白色光束从舞台两侧向大屏汇聚",
            }]
            detail_rows = [{
                "slot_index": 4,
                "模板镜号": "4",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 27,
                "匹配理由": "匹配分 100",
                "输出图片来源": "创意库",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            analysis = {
                "exterior.png": {
                    "summary": "蓝色外场建筑与 Midea 门头，属于主会场外观",
                    "tags": ["外场", "建筑", "Midea门头", "主会场外观"],
                    "source": "人工复核",
                }
            }

            detail_rows, visual_assets = apply_visual_analysis(script_rows, detail_rows, visual_assets, analysis)

        self.assertEqual(visual_assets[0]["配图判断"], "不匹配")
        self.assertIn("内场舞台/大屏", visual_assets[0]["冲突视觉要素"])

    def test_visual_analysis_rejects_wrong_brand_wall_for_midea_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "cleer.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            script_rows = [{
                "镜号": "5",
                "画面示意": str(image_path),
                "画面描述": "签到墙顾客互动",
            }]
            detail_rows = [{
                "slot_index": 5,
                "模板镜号": "5",
                "项目名称": "美的智美万象发布会开场短片",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 30,
                "匹配理由": "匹配分 100",
                "输出图片来源": "创意库",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            analysis = {
                "cleer.png": {
                    "summary": "人物在白色品牌墙前互动，墙面出现 Cleer 字样，非 Midea",
                    "tags": ["人物", "互动", "品牌墙", "Cleer", "打卡"],
                    "source": "人工复核",
                }
            }

            detail_rows, visual_assets = apply_visual_analysis(script_rows, detail_rows, visual_assets, analysis)

        self.assertEqual(visual_assets[0]["配图判断"], "不匹配")
        self.assertIn("其他品牌", visual_assets[0]["冲突视觉要素"])

    def test_visual_analysis_rejects_car_exhibit_as_midea_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "car_stage.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            script_rows = [{
                "镜号": "4",
                "画面示意": str(image_path),
                "画面描述": "主会场空镜，多束白色光束从舞台两侧向大屏汇聚",
            }]
            detail_rows = [{
                "slot_index": 4,
                "模板镜号": "4",
                "项目名称": "美的智美万象发布会开场短片",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 27,
                "匹配理由": "匹配分 100",
                "输出图片来源": "创意库",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            analysis = {
                "car_stage.png": {
                    "summary": "COLMO 展区汽车与环形大屏装置，属于汽车展区场景",
                    "tags": ["COLMO", "汽车", "展区", "环形装置", "大屏"],
                    "source": "人工复核",
                }
            }

            detail_rows, visual_assets = apply_visual_analysis(script_rows, detail_rows, visual_assets, analysis)

        self.assertEqual(visual_assets[0]["配图判断"], "不匹配")
        self.assertIn("汽车展区", visual_assets[0]["冲突视觉要素"])

    def test_rematch_visual_assets_replaces_mismatch_with_better_pool_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "02.png"
            good = Path(tmp) / "07.png"
            Image.new("RGB", (1920, 1080), color="white").save(bad)
            Image.new("RGB", (1920, 1080), color="white").save(good)
            script_rows = [{
                "镜号": "1",
                "画面示意": str(bad),
                "效果示意": "",
                "画面描述": "美的品牌logo穿梭入画",
            }]
            detail_rows = [{
                "slot_index": 1,
                "模板镜号": "1",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 8,
                "图片证据类型": "项目编号回退",
                "匹配理由": "匹配分 100",
                "输出图片来源": "创意库",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            analysis = {
                "02.png": {
                    "summary": "蓝色外场建筑与 Midea 门头",
                    "tags": ["外场", "建筑", "Midea门头"],
                    "source": "人工复核",
                },
                "07.png": {
                    "summary": "Midea logo 标识特写",
                    "tags": ["logo", "品牌标识", "Midea", "标识特写"],
                    "source": "人工复核",
                },
            }
            detail_rows, visual_assets = apply_visual_analysis(script_rows, detail_rows, visual_assets, analysis)

            script_rows, detail_rows, visual_assets = rematch_visual_assets(
                script_rows,
                detail_rows,
                visual_assets,
                analysis,
                [bad, good],
            )

        self.assertEqual(script_rows[0]["画面示意"], str(good))
        self.assertEqual(visual_assets[0]["绑定方式"], "视觉重配")
        self.assertEqual(visual_assets[0]["配图判断"], "匹配")
        self.assertEqual(detail_rows[0]["图片绑定方式"], "视觉重配")
        self.assertEqual(detail_rows[0]["配图置信度"], "高")

    def test_rematch_visual_assets_prefers_specific_channel_crowd_over_generic_audience(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.png"
            generic = Path(tmp) / "generic.png"
            specific = Path(tmp) / "specific.png"
            for path in (bad, generic, specific):
                Image.new("RGB", (1920, 1080), color="white").save(path)
            script_rows = [{
                "镜号": "9",
                "画面示意": str(bad),
                "效果示意": "",
                "画面描述": "展馆通道内熙熙攘攘的参观人群",
            }]
            detail_rows = [{
                "slot_index": 9,
                "模板镜号": "9",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 34,
                "匹配理由": "匹配分 100",
                "输出图片来源": "创意库",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            analysis = {
                "bad.png": {"summary": "车机屏幕特写", "tags": ["车机", "屏幕"], "source": "人工复核"},
                "generic.png": {"summary": "主会场观众席", "tags": ["观众", "主会场"], "source": "人工复核"},
                "specific.png": {
                    "summary": "室内展区通道与人群，观众在通道内参观交流",
                    "tags": ["人群", "观众", "通道", "参观", "展区"],
                    "source": "人工复核",
                },
            }
            detail_rows, visual_assets = apply_visual_analysis(script_rows, detail_rows, visual_assets, analysis)

            script_rows, detail_rows, visual_assets = rematch_visual_assets(
                script_rows,
                detail_rows,
                visual_assets,
                analysis,
                [bad, generic, specific],
            )

        self.assertEqual(script_rows[0]["画面示意"], str(specific))
        self.assertEqual(Path(visual_assets[0]["原图路径"]).name, "specific.png")

    def test_rematch_visual_assets_upgrades_low_res_match_to_high_res_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            low_res = Path(tmp) / "low.png"
            high_res = Path(tmp) / "high.png"
            Image.new("RGB", (550, 309), color="white").save(low_res)
            Image.new("RGB", (1920, 1080), color="white").save(high_res)
            script_rows = [{
                "镜号": "4",
                "画面示意": str(low_res),
                "效果示意": "",
                "画面描述": "主会场空镜，大屏呈现蓝色发布会主视觉",
            }]
            detail_rows = [{
                "slot_index": 4,
                "模板镜号": "4",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 27,
                "匹配理由": "匹配分 100",
                "输出图片来源": "创意库",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            analysis = {
                "low.png": {
                    "summary": "主会场大屏与观众席，蓝色发布会主视觉",
                    "tags": ["主会场", "大屏", "发布会现场", "主视觉"],
                    "source": "人工复核",
                },
                "high.png": {
                    "summary": "主会场大屏与观众席，蓝色发布会主视觉",
                    "tags": ["主会场", "大屏", "发布会现场", "主视觉"],
                    "source": "人工复核",
                },
            }
            detail_rows, visual_assets = apply_visual_analysis(script_rows, detail_rows, visual_assets, analysis)

            script_rows, detail_rows, visual_assets = rematch_visual_assets(
                script_rows,
                detail_rows,
                visual_assets,
                analysis,
                [low_res, high_res],
            )

        self.assertEqual(script_rows[0]["画面示意"], str(high_res))
        self.assertEqual(visual_assets[0]["原图宽"], 1920)
        self.assertEqual(visual_assets[0]["质量指标"], "高清原图")
        self.assertEqual(detail_rows[0]["图片绑定方式"], "视觉重配")

    def test_write_template_output_workbook_adds_visual_asset_sheet_with_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "01.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            output = Path(tmp) / "template_output.xlsx"
            script_rows = [{
                "结构": "开场造势",
                "镜号": "1",
                "画面示意": str(image_path),
                "效果示意": "",
                "效果": "",
                "类型": "",
                "景别": "",
                "运镜": "",
                "画面描述": "品牌logo穿梭入画",
                "声音": "",
                "备注": "",
            }]
            detail_rows = [{
                "slot_index": 1,
                "模板镜号": "1",
                "替换方式": "REPLACE",
                "替换来源文件": "M+发布会类通用脚本库.xlsx",
                "替换来源 sheet": "创意库",
                "替换来源行": 8,
                "匹配理由": "匹配分 100；槽位画面匹配:品牌、logo",
                "输出图片来源": "创意库",
            }]
            enriched_details, visual_assets = attach_visual_assets(script_rows, detail_rows)
            enriched_details, visual_assets = apply_visual_analysis(
                script_rows,
                enriched_details,
                visual_assets,
                {
                    visual_assets[0]["visual_asset_id"]: {
                        "caption": "美的发布会品牌墙",
                        "tags": ["美的", "品牌标识"],
                        "ocr_text": "Midea",
                        "brand_names": ["美的"],
                        "negative_flags": ["无"],
                        "source": "minimax",
                    }
                },
            )

            write_template_output_workbook(
                output,
                script_rows,
                enriched_details,
                [],
                template_workbook_path=anonymous_launch_template(Path(tmp) / "source"),
                visual_asset_rows=visual_assets,
            )
            wb = load_workbook(output)
            ws = wb["视觉资产"]

            self.assertEqual([cell.value for cell in ws[1]], VISUAL_ASSET_COLUMNS)
            headers = [ws.cell(1, col).value for col in range(1, ws.max_column + 1)]
            row = {headers[index]: ws.cell(2, index + 1).value for index in range(len(headers))}
            self.assertEqual(row["visual_asset_id"], visual_assets[0]["visual_asset_id"])
            self.assertEqual(row["原图路径"], str(image_path))
            self.assertEqual(row["原图宽"], 1920)
            self.assertEqual(row["原图高"], 1080)
            self.assertEqual(row["ocr_text"], "Midea")
            self.assertEqual(row["brand_names"], "美的")
            self.assertEqual(row["negative_flags"], "无")
            self.assertEqual(len(ws._images), 0)
            self.assertIn("配图复核", wb.sheetnames)
            review_ws = wb["配图复核"]
            self.assertEqual([cell.value for cell in review_ws[1]], PAIRING_REVIEW_COLUMNS)
            self.assertEqual(review_ws["A2"].value, "1")
            self.assertEqual(len(review_ws._images), 0)
            wb.close()

    def test_external_visual_text_is_stored_as_text_not_excel_formula(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "formula_safe.xlsx"
            formula = '=HYPERLINK("https://example.invalid","click")'
            script_rows = [{
                "结构": "开场造势",
                "镜号": "1",
                "画面描述": formula,
            }]
            detail_rows = [{
                "slot_index": 1,
                "模板镜号": "1",
                "视觉内容摘要": formula,
            }]
            visual_assets = [{
                "visual_asset_id": "asset-1",
                "image_id": "image-1",
                "slot_index": 1,
                "镜号": "1",
                "视觉内容摘要": formula,
                "视觉标签": formula,
                "ocr_text": formula,
                "brand_names": formula,
                "negative_flags": formula,
            }]

            write_template_output_workbook(
                output,
                script_rows,
                detail_rows,
                [],
                visual_asset_rows=visual_assets,
            )
            wb = load_workbook(output, data_only=False)
            for sheet_name, columns in (
                ("脚本", ("画面描述",)),
                ("抽调明细", ("视觉内容摘要",)),
                ("视觉资产", (
                    "视觉内容摘要", "视觉标签", "ocr_text", "brand_names", "negative_flags",
                )),
            ):
                ws = wb[sheet_name]
                headers = [cell.value for cell in ws[1]]
                for column in columns:
                    cell = ws.cell(2, headers.index(column) + 1)
                    self.assertEqual(cell.value, formula)
                    self.assertEqual(cell.data_type, "s")
            wb.close()

    def test_visual_evidence_audit_warns_replacement_without_asset_id(self):
        checks = audit_visual_evidence([{
            "slot_index": 1,
            "模板镜号": "1",
            "替换方式": "REPLACE",
            "替换来源文件": "M+发布会类通用脚本库.xlsx",
            "替换来源 sheet": "创意库",
            "替换来源行": 8,
            "匹配理由": "匹配分 100",
            "视觉资产ID": "",
        }])

        self.assertIn("missing_visual_asset", {check["检查项"] for check in checks if check["级别"] == "warning"})

    def test_visual_evidence_audit_warns_visual_mismatch(self):
        checks = audit_visual_evidence([{
            "slot_index": 1,
            "模板镜号": "1",
            "替换方式": "REPLACE",
            "视觉资产ID": "va_test",
            "图片绑定方式": "行绑定",
            "图片绑定说明": "视觉分析判断: 不匹配",
            "视觉质量指标": "高清原图",
            "视觉读取状态": "人工复核",
            "配图判断": "不匹配",
            "视觉匹配分": 37,
        }])

        self.assertIn("visual_pairing_mismatch", {check["检查项"] for check in checks if check["级别"] == "warning"})

    def test_visual_evidence_audit_warns_on_weak_visual_match(self):
        checks = audit_visual_evidence([{
            "slot_index": 1,
            "模板镜号": "1",
            "替换方式": "REPLACE",
            "视觉资产ID": "va_test",
            "图片绑定方式": "行绑定",
            "图片绑定说明": "视觉分析判断: 弱匹配",
            "视觉质量指标": "高清原图",
            "视觉读取状态": "人工复核",
            "配图判断": "弱匹配",
            "视觉匹配分": 72,
        }])

        self.assertIn("visual_pairing_weak_match", {check["检查项"] for check in checks if check["级别"] == "warning"})

    def test_visual_evidence_audit_accepts_numbered_fallback_after_minimax_verification(self):
        checks = audit_visual_evidence([{
            "slot_index": 1,
            "模板镜号": "1",
            "替换方式": "REPLACE",
            "视觉资产ID": "va_test",
            "image_id": "img_test",
            "图片绑定方式": "编号回退",
            "图片绑定说明": "项目编号回退后已由 MiniMax 读取",
            "视觉质量指标": "高清原图",
            "视觉读取状态": "minimax",
            "配图判断": "匹配",
            "视觉匹配分": 100,
        }])

        items = {check["检查项"] for check in checks}
        self.assertNotIn("numbered_image_fallback_needs_review", items)

    def test_visual_evidence_audit_warns_duplicate_image_outside_transition_callback(self):
        rows = [
            {
                "slot_index": slot,
                "模板镜号": str(slot),
                "替换方式": "REPLACE",
                "视觉资产ID": f"va_{slot}",
                "image_id": "img_shared",
                "图片绑定方式": "视觉重配",
                "图片绑定说明": "MiniMax 视觉匹配",
                "视觉读取状态": "minimax",
                "配图判断": "匹配",
                "视觉匹配分": 90,
            }
            for slot in (15, 16)
        ]

        checks = audit_visual_evidence(rows)

        self.assertIn("duplicate_visual_asset", {check["检查项"] for check in checks if check["级别"] == "warning"})

    def test_visual_evidence_audit_warns_duplicate_image_for_keep_slots(self):
        rows = [
            {
                "slot_index": slot,
                "模板镜号": str(slot),
                "替换方式": "KEEP",
                "image_id": "img_shared",
            }
            for slot in (15, 16)
        ]

        checks = audit_visual_evidence(rows)

        self.assertIn("duplicate_visual_asset", {check["检查项"] for check in checks if check["级别"] == "warning"})

    def test_visual_evidence_audit_allows_shared_transition_image_for_slots_3_and_10(self):
        rows = [
            {
                "slot_index": slot,
                "模板镜号": str(slot),
                "替换方式": "REPLACE",
                "视觉资产ID": f"va_{slot}",
                "image_id": "img_transition",
                "图片绑定方式": "视觉重配",
                "图片绑定说明": "品牌视觉回扣",
                "视觉读取状态": "minimax",
                "配图判断": "匹配",
                "视觉匹配分": 90,
            }
            for slot in (3, 10)
        ]

        checks = audit_visual_evidence(rows)

        self.assertNotIn("duplicate_visual_asset", {check["检查项"] for check in checks})

    def test_candidate_visual_coverage_warns_when_pool_is_partially_unread(self):
        with tempfile.TemporaryDirectory() as tmp:
            read_image = Path(tmp) / "read.png"
            unread_image = Path(tmp) / "unread.png"
            Image.new("RGB", (1920, 1080), color="white").save(read_image)
            Image.new("RGB", (1920, 1080), color="white").save(unread_image)

            checks = audit_candidate_visual_coverage(
                {"read.png": {"summary": "Midea logo", "tags": ["logo"], "source": "人工复核"}},
                [read_image, unread_image],
            )

        self.assertEqual(checks[0]["检查项"], "visual_candidate_pool_partially_unread")
        self.assertIn("1/2", checks[0]["详情"])
        self.assertIn("unread.png", checks[0]["详情"])

    def test_write_visual_review_html_references_original_image_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "01.png"
            Image.new("RGB", (1920, 1080), color="white").save(image_path)
            output = Path(tmp) / "template_output.xlsx"
            html = Path(tmp) / "review.html"
            script_rows = [{
                "结构": "开场造势",
                "镜号": "1",
                "画面示意": str(image_path),
                "效果示意": "",
                "效果": "",
                "类型": "",
                "景别": "",
                "运镜": "",
                "画面描述": "品牌logo穿梭入画",
                "声音": "",
                "备注": "",
            }]
            detail_rows = [{
                "slot_index": 1,
                "模板镜号": "1",
                "替换方式": "REPLACE",
                "替换来源 sheet": "创意库",
                "替换来源行": 8,
                "匹配理由": "匹配分 100",
                "输出图片来源": "创意库",
            }]
            detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
            visual_assets[0]["视觉读取状态"] = "人工复核"
            visual_assets[0]["视觉内容摘要"] = "Midea logo 标识特写"
            visual_assets[0]["配图判断"] = "匹配"
            visual_assets[0]["视觉匹配分"] = 100
            write_template_output_workbook(
                output,
                script_rows,
                detail_rows,
                [],
                visual_asset_rows=visual_assets,
            )

            written = write_visual_review_html_from_workbook(output, html)
            text = written.read_text(encoding="utf-8")

        self.assertEqual(written, html)
        self.assertIn("data:image/png;base64,", text)
        self.assertNotIn(image_path.as_uri(), text)
        self.assertIn("Midea logo 标识特写", text)

    def test_audit_generation_fails_structure_mismatch_and_suggests_constraints(self):
        slot = source_shot(source_sheet="模版", slot_index=1, structure="开场造势", visual_desc="品牌logo穿梭入画")
        script_rows = []
        detail_rows = []

        result = audit_generation(
            {"禁忌或不能出现的内容": "竞品"},
            [slot],
            [],
            script_rows,
            detail_rows,
        )

        self.assertEqual(result["status"], "fail")
        self.assertGreater(result["hard_fail_count"], 0)
        self.assertIn("structure_mismatch", result["blocking_items"])
        self.assertIn("force_template_order", result["next_constraints"])

    def test_audit_real_launch_requires_explicit_60_second_allocation(self):
        brief = read_text_brief(FIXED_REAL_BRIEF_TEXT)
        slots = [
            source_shot(
                source_sheet="模版",
                source_row=6 + index,
                slot_index=index,
                shot_no=str(index),
                structure="开场造势",
            )
            for index in range(1, 19)
        ]
        script_rows = [
            {
                "结构": "开场造势",
                "镜号": str(index),
                "画面描述": (
                    "美的品牌logo、活动主会场、智美万象主题字样、装置空镜、"
                    "人流互动、核心发布仪式"
                    if index == 1 else "发布会现场镜头"
                ),
                "备注": "",
            }
            for index in range(1, 19)
        ]

        result = audit_generation(brief, slots, [], script_rows, [{} for _ in slots])

        hard_items = {check["检查项"] for check in result["checks"] if check["级别"] == "hard"}
        self.assertIn("duration_total_mismatch", hard_items)

    def test_audit_real_launch_requires_all_brief_must_have_content(self):
        brief = read_text_brief(FIXED_REAL_BRIEF_TEXT)
        slots = [
            source_shot(
                source_sheet="模版",
                source_row=6 + index,
                slot_index=index,
                shot_no=str(index),
                structure="开场造势",
            )
            for index in range(1, 19)
        ]
        script_rows = [
            {
                "结构": "开场造势",
                "镜号": str(index),
                "画面描述": "美的品牌logo、活动主会场、智美万象主题字样、装置空镜、人流互动",
                "时长(秒)": 3 if index < 18 else 9,
                "备注": f"建议时长：{3 if index < 18 else 9}s",
            }
            for index in range(1, 19)
        ]

        result = audit_generation(brief, slots, [], script_rows, [{} for _ in slots])

        checks = [check for check in result["checks"] if check["检查项"] == "required_content_missing"]
        self.assertEqual(len(checks), 1)
        self.assertIn("核心发布仪式", checks[0]["详情"])

    def test_audit_allows_cross_brand_creative_source_but_blocks_other_brand_in_final_script(self):
        brief = read_text_brief(FIXED_REAL_BRIEF_TEXT)
        slots = [
            source_shot(
                source_sheet="模版",
                source_row=6 + index,
                slot_index=index,
                shot_no=str(index),
                structure="开场造势",
            )
            for index in range(1, 19)
        ]
        script_rows = [
            {
                "结构": "开场造势",
                "镜号": str(index),
                "画面描述": (
                    "Cleer品牌logo、活动主会场、智美万象主题字样、装置空镜、人流互动、核心发布仪式"
                    if index == 1 else "美的发布会现场"
                ),
                "时长(秒)": 3 if index < 18 else 9,
                "备注": f"建议时长：{3 if index < 18 else 9}s",
            }
            for index in range(1, 19)
        ]
        decisions = [
            TemplateDecision(
                slot=slots[0],
                variant=source_shot(visual_desc="Cleer品牌logo创意转场"),
                mode="REPLACE",
                score=90,
                match_reason="镜头功能符合品牌开场",
            )
        ]

        result = audit_generation(brief, slots, decisions, script_rows, [{} for _ in slots])

        hard_items = {check["检查项"] for check in result["checks"] if check["级别"] == "hard"}
        self.assertIn("output_brand_conflict", hard_items)

    def test_audit_blocks_midea_text_in_linsy_final_script(self):
        brief = {
            "项目名称": "林氏家居生活方式发布会",
            "品牌或客户": "林氏家居",
            "脚本类型": "发布会",
            "视频时长": "60s",
            "必须出现的内容": "品牌logo、活动主会场、人流互动、核心发布仪式",
            "禁忌或不能出现的内容": "",
        }
        slots = [
            source_shot(
                source_sheet="模版",
                source_row=6 + index,
                slot_index=index,
                shot_no=str(index),
                structure="开场造势",
            )
            for index in range(1, 19)
        ]
        script_rows = [
            {
                "结构": "开场造势",
                "镜号": str(index),
                "画面描述": (
                    "Midea品牌logo、活动主会场、人流互动、核心发布仪式"
                    if index == 1 else "林氏家居发布会现场"
                ),
                "时长(秒)": 3 if index < 18 else 9,
                "备注": f"建议时长：{3 if index < 18 else 9}s",
            }
            for index in range(1, 19)
        ]

        result = audit_generation(brief, slots, [], script_rows, [{} for _ in slots])

        hard_items = {check["检查项"] for check in result["checks"] if check["级别"] == "hard"}
        self.assertIn("output_brand_conflict", hard_items)

    def test_audit_generation_fails_missing_replacement_source_and_slot_conflict(self):
        slot = source_shot(source_sheet="模版", slot_index=1, structure="开场造势", visual_desc="品牌logo穿梭入画")
        bad_variant = source_shot(source_sheet="创意库", source_row=41, visual_desc="品牌logo穿梭入画，主持人报幕")
        decisions = [TemplateDecision(
            slot=slot,
            variant=bad_variant,
            mode="REPLACE",
            score=90,
            match_reason="模拟损坏的上游决策",
        )]
        script_rows, detail_rows = generate_template_rows({
            "项目名称": "测试发布会", "input_row": 1, "视频时长": "60s",
        }, decisions)
        detail_rows[0]["替换来源行"] = ""

        result = audit_generation(
            {"视频时长": "60s", "禁忌或不能出现的内容": ""},
            [slot],
            decisions,
            script_rows,
            detail_rows,
        )

        self.assertEqual(result["status"], "fail")
        self.assertIn("missing_source_trace", result["blocking_items"])
        self.assertIn("slot_conflict", result["blocking_items"])
        self.assertEqual(result["next_constraints"]["force_keep_slots"], [1])
        self.assertEqual(result["next_constraints"]["exclude_by_slot"]["1"][0]["source_row"], 41)

    def test_audit_allows_slot_conflict_from_same_shot_confirmed_brief_event(self):
        slot = source_shot(
            source_sheet="模版",
            slot_index=2,
            structure="活动收束",
            visual_desc="全场大合影",
        )
        script_rows, detail_rows = generate_template_rows({
            "项目名称": "测试发布会", "input_row": 1, "视频时长": "60s",
        }, [TemplateDecision(
            slot=slot,
            variant=None,
            mode="KEEP",
            score=100,
            match_reason="同镜确认 Brief 流程事件",
        )])
        script_rows[0]["画面描述"] = "航拍镜头展现全场大合影"
        detail_rows[0]["规划主事件"] = "全场大合影(航拍镜头)"

        result = audit_generation(
            {"视频时长": "60s", "禁忌或不能出现的内容": ""},
            [slot],
            [TemplateDecision(
                slot=slot,
                variant=None,
                mode="KEEP",
                score=100,
                match_reason="同镜确认 Brief 流程事件",
            )],
            script_rows,
            detail_rows,
        )

        self.assertNotIn("slot_conflict", result["blocking_items"])
        warning_items = {
            check["检查项"] for check in result["checks"] if check["级别"] == "warning"
        }
        self.assertIn("slot_conflict_brief_override", warning_items)

    def test_audit_treats_confirmed_brand_wrap_as_closing_override(self):
        slot = source_shot(
            source_sheet="模版", slot_index=8,
            structure="品牌落版", visual_desc="品牌收束",
        )
        decision = TemplateDecision(
            slot=slot, variant=None, mode="KEEP", score=100,
            match_reason="同镜确认 Brief 流程事件",
        )
        script_rows, detail_rows = generate_template_rows({
            "项目名称": "测试发布会", "input_row": 1, "视频时长": "60s",
        }, [decision])
        script_rows[0]["画面描述"] = "品牌落版完成整片收束"
        detail_rows[0]["规划主事件"] = "品牌收束"

        result = audit_generation(
            {"视频时长": "60s", "禁忌或不能出现的内容": ""},
            [slot], [decision], script_rows, detail_rows,
        )

        self.assertNotIn("slot_conflict", result["blocking_items"])
        self.assertTrue(any(
            check["检查项"] == "slot_conflict_brief_override"
            for check in result["checks"]
        ))

    def test_audit_generation_aggregates_missing_reuse_metadata_by_source(self):
        slots = [
            source_shot(source_sheet="模版", slot_index=1, structure="开场造势", visual_desc="品牌logo穿梭入画"),
            source_shot(source_sheet="模版", slot_index=2, structure="开场造势", visual_desc="搭建延时"),
        ]
        variants = [
            source_shot(source_sheet="创意库", source_row=8, structure="开场造势", visual_desc="品牌logo穿梭入画"),
            source_shot(source_sheet="创意库", source_row=13, structure="开场造势", visual_desc="搭建延时"),
        ]
        decisions = match_template_slots({
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "品牌logo",
            "禁忌或不能出现的内容": "",
        }, slots, variants)
        script_rows, detail_rows = generate_template_rows({
            "项目名称": "测试发布会", "input_row": 1, "视频时长": "60s",
        }, decisions)

        result = audit_generation({}, slots, decisions, script_rows, detail_rows)
        metadata_warnings = [
            check for check in result["checks"]
            if check["检查项"] == "missing_reuse_metadata"
        ]

        self.assertEqual(len(metadata_warnings), 1)
        self.assertIn("可复用等级", metadata_warnings[0]["详情"])
        self.assertIn("禁用说明", metadata_warnings[0]["详情"])

    def test_audit_allows_duplicate_source_for_repeated_transition_slots(self):
        slots = [
            source_shot(source_sheet="模版", slot_index=3, structure="核心宣讲与仪式", visual_desc="品牌logo穿越机转场"),
            source_shot(source_sheet="模版", slot_index=10, structure="核心宣讲与仪式", visual_desc="品牌logo穿越机转场"),
        ]
        variant = source_shot(source_sheet="创意库", source_row=39, structure="核心宣讲", visual_desc="品牌logo穿越机转场")
        decisions = match_template_slots({
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "品牌logo",
            "禁忌或不能出现的内容": "",
        }, slots, [variant])
        script_rows, detail_rows = generate_template_rows({
            "项目名称": "测试发布会", "input_row": 1, "视频时长": "60s",
        }, decisions)

        result = audit_generation({}, slots, decisions, script_rows, detail_rows)

        self.assertFalse(any(check["检查项"] == "duplicate_source" for check in result["checks"]))

    def test_write_template_output_workbook_can_include_audit_sheet_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "template_output.xlsx"
            audit_path = Path(tmp) / "template_output.xlsx.audit.json"
            result = {
                "status": "pass",
                "hard_fail_count": 0,
                "warning_count": 1,
                "score": 95,
                "blocking_items": [],
                "next_constraints": {},
                "checks": [{
                    "检查项": "missing_image",
                    "级别": "warning",
                    "状态": "warning",
                    "关联镜号": "1",
                    "详情": "无可用嵌入图",
                    "修复建议": "补充图片锚点",
                }],
            }

            write_template_output_workbook(path, [], [], audit_rows(result))
            write_audit_json(audit_path, result)
            wb = load_workbook(path, read_only=True)

            self.assertEqual(
                wb.sheetnames,
                ["脚本", "抽调明细", "剪辑复核", "相邻镜头复核", "整片复核", "质量审计"],
            )
            self.assertEqual([cell.value for cell in wb["质量审计"][1]], AUDIT_COLUMNS)
            wb.close()
            self.assertEqual(json.loads(audit_path.read_text(encoding="utf-8"))["status"], "pass")

    def test_write_template_output_workbook_embeds_local_images_in_script_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "shot.png"
            Image.new("RGB", (80, 40), color="white").save(image_path)
            output = Path(tmp) / "template_output.xlsx"

            write_template_output_workbook(output, [{
                "结构": "开场造势",
                "镜号": "1",
                "画面示意": str(image_path),
                "效果示意": "",
                "效果": "",
                "类型": "",
                "景别": "",
                "运镜": "",
                "画面描述": "品牌logo穿梭入画",
                "声音": "",
                "备注": "",
            }], [], [])
            wb = load_workbook(output)

            self.assertEqual(len(wb["脚本"]._images), 1)
            wb.close()

    def test_output_format_audit_rejects_flat_workbook_with_unembedded_image_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "shot.png"
            Image.new("RGB", (80, 40), color="white").save(image_path)
            output = Path(tmp) / "flat.xlsx"
            write_template_output_workbook(output, [{
                "结构": "开场造势",
                "镜号": "1",
                "画面示意": str(image_path),
                "效果示意": "",
                "效果": "",
                "类型": "",
                "景别": "",
                "运镜": "",
                "画面描述": "品牌logo穿梭入画",
                "声音": "",
                "备注": "",
            }], [], [])

            checks = audit_output_workbook_format(output, expected_image_count=1)

            hard_fail_items = {check["检查项"] for check in checks if check["级别"] == "hard"}
            self.assertIn("template_layout_missing", hard_fail_items)
            self.assertIn("image_path_not_embedded", hard_fail_items)

    def test_output_format_audit_accepts_template_like_embedded_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "shot.png"
            Image.new("RGB", (320, 180), color="white").save(image_path)
            output = Path(tmp) / "template.xlsx"
            write_template_output_workbook(
                output,
                [{
                    "结构": "开场造势",
                    "镜号": "1",
                    "画面示意": str(image_path),
                    "效果示意": "",
                    "效果": "",
                    "类型": "",
                    "景别": "",
                    "运镜": "",
                    "画面描述": "品牌logo穿梭入画",
                    "声音": "",
                    "备注": "",
                }],
                [],
                [],
                template_workbook_path=anonymous_launch_template(Path(tmp) / "source"),
            )

            checks = audit_output_workbook_format(output, expected_image_count=1)

            self.assertFalse([check for check in checks if check["级别"] == "hard"])

    def test_match_template_slots_applies_loop_constraints(self):
        slot = source_shot(source_sheet="模版", slot_index=1, structure="开场造势", visual_desc="品牌logo穿梭入画")
        excluded = source_shot(source_sheet="创意库", source_row=8, structure="开场造势", visual_desc="品牌logo穿梭入画，主持人报幕")
        allowed = source_shot(source_sheet="创意库", source_row=9, structure="开场造势", visual_desc="品牌logo穿梭入画")

        decisions = match_template_slots({
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "品牌logo",
            "禁忌或不能出现的内容": "",
        }, [slot], [excluded, allowed], constraints={
            "exclude_by_slot": {"1": [{"source_file": "launch.xlsx", "source_sheet": "创意库", "source_row": 8}]},
        })

        self.assertEqual(decisions[0].variant.source_row, 9)

    def test_match_template_slots_hard_filters_slot_conflict_before_explanation(self):
        slot = source_shot(
            source_sheet="模版",
            slot_index=7,
            structure="人气与现场互动",
            visual_desc="主题空间体验，客户互动",
        )
        variant = source_shot(
            source_sheet="创意库",
            source_row=21,
            structure="人气与现场互动",
            visual_desc="主题空间互动体验，但混入签到动作",
        )

        decisions = match_template_slots({
            "脚本类型": "发布会",
            "风格调性": "科技感",
            "必须出现的内容": "主题空间互动",
            "禁忌或不能出现的内容": "",
        }, [slot], [variant])

        self.assertEqual(decisions[0].mode, "KEEP")
        self.assertIsNone(decisions[0].variant)
        self.assertIn("无同结构创意库候选", decisions[0].selected_reason)

    def test_generation_loop_passes_first_round_and_writes_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            write_full_launch_media(root)
            write_launch_library(root / "M+发布会类通用脚本库.xlsx", full_launch_template_rows(), [])
            output = Path(tmp) / "out.xlsx"
            brief = read_text_brief(FIXED_REAL_BRIEF_TEXT)

            result = run_generation_loop(
                [brief], root, output, max_loops=3,
                vision_analysis=full_launch_vision_analysis(),
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.passed_iteration, 1)
            self.assertTrue(output.exists())
            self.assertTrue(Path(str(output) + ".audit.json").exists())
            self.assertTrue(result.log_path.exists())
            loop_log = json.loads(result.log_path.read_text(encoding="utf-8"))
            self.assertEqual(loop_log["status"], "pass")
            self.assertEqual(loop_log["iterations"][0]["phase"], "generate-audit-diagnose")
            self.assertEqual(loop_log["iterations"][0]["hard_fail_count"], 0)

    def test_generation_once_keeps_brief_copy_and_records_visual_evidence_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            write_full_launch_media(root)
            write_launch_library(root / "M+发布会类通用脚本库.xlsx", full_launch_template_rows(), [])
            output = Path(tmp) / "out.xlsx"
            brief = read_text_brief(FIXED_REAL_BRIEF_TEXT)
            analysis = {
                "01.png": {
                    "summary": "Midea品牌logo聚光灯束聚焦中央圆形光幕",
                    "tags": ["Midea", "logo", "品牌标识", "聚光灯"],
                    "source": "minimax",
                }
            }

            iteration = run_generation_once(
                [brief],
                root,
                output,
                vision_analysis=analysis,
            )
            wb = load_workbook(iteration.output_path, data_only=True)
            ws = wb["脚本"]
            description = ws["I7"].value
            note = ws["K7"].value
            visual_ws = wb["视觉资产"]
            visual_headers = [cell.value for cell in visual_ws[1]]
            summary_col = visual_headers.index("视觉内容摘要") + 1
            visual_evidence = visual_ws.cell(2, summary_col).value
            wb.close()

        self.assertNotIn("Midea品牌logo聚光灯", description)
        self.assertIn("美的品牌logo", description)
        self.assertIn("Midea品牌logo聚光灯", visual_evidence)
        self.assertIn("建议时长：3s", note)

    def test_visual_mismatch_produces_executable_next_loop_constraint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            write_launch_media(root, "01.png")
            write_launch_library(root / "M+发布会类通用脚本库.xlsx", full_launch_template_rows(), [
                [1, "开场造势", "A", None, "字体穿梭", "航拍", "全景", "固定", "品牌logo穿梭入画", "", ""],
            ])
            output = Path(tmp) / "out.xlsx"
            brief = read_text_brief(FIXED_REAL_BRIEF_TEXT)
            analysis = {
                "01.png": {
                    "summary": "发布会外场建筑入口，没有清晰品牌标识",
                    "tags": ["外场", "建筑", "入口"],
                    "source": "minimax",
                }
            }

            iteration = run_generation_once(
                [brief],
                root,
                output,
                vision_analysis=analysis,
            )

        self.assertIn("exclude_visuals_by_slot", iteration.audit["next_constraints"])
        excluded = iteration.audit["next_constraints"]["exclude_visuals_by_slot"]["1"]
        self.assertTrue(any("01.png" in item for item in excluded))

    def test_generation_loop_hard_filters_bad_candidate_before_first_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            write_launch_media(root, "01.png", "02.png")
            write_launch_library(root / "M+发布会类通用脚本库.xlsx", full_launch_template_rows(), [
                [1, "开场造势", "A", None, "字体穿梭", "航拍", "全景", "固定", "品牌logo穿梭入画，主持人报幕", "", ""],
                [2, "开场造势", "B", None, "", "", "", "", "品牌logo穿梭入画", "", ""],
            ])
            output = Path(tmp) / "out.xlsx"
            brief = read_text_brief(FIXED_REAL_BRIEF_TEXT)

            result = run_generation_loop(
                [brief], root, output, max_loops=3,
                vision_analysis=full_launch_vision_analysis(),
            )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.passed_iteration, 1)
            self.assertTrue((Path(tmp) / "out.loop1.audit.json").exists())
            audit = json.loads(Path(str(output) + ".audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["status"], "pass")

    def test_generation_loop_exits_early_on_repeated_unfixable_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty_source"
            root.mkdir()
            output = Path(tmp) / "out.xlsx"
            vision_path = Path(tmp) / "vision.json"
            vision_path.write_text(
                json.dumps(full_launch_vision_analysis(), ensure_ascii=False),
                encoding="utf-8",
            )
            brief = read_text_brief(FIXED_REAL_BRIEF_TEXT)

            result = run_generation_loop([brief], root, output, max_loops=5)

            self.assertEqual(result.exit_code, 2)
            self.assertEqual(len(result.iterations), 2)
            self.assertTrue((Path(tmp) / "out.best_failed.xlsx").exists())
            self.assertTrue((Path(tmp) / "out.best_failed.audit.json").exists())
            loop_log = json.loads(result.log_path.read_text(encoding="utf-8"))
            self.assertEqual(loop_log["status"], "failed")
            self.assertEqual(loop_log["stop_reason"], "repeated_failure_signature")

    def test_cli_loop_writes_round_files_and_returns_zero_when_audit_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            brief_path = Path(tmp) / "brief.txt"
            brief_path.write_text(FIXED_REAL_BRIEF_TEXT, encoding="utf-8")
            write_full_launch_media(root)
            write_launch_library(root / "M+发布会类通用脚本库.xlsx", full_launch_template_rows(), [])
            output = Path(tmp) / "out.xlsx"
            vision_path = Path(tmp) / "vision.json"
            vision_path.write_text(
                json.dumps(full_launch_vision_analysis(), ensure_ascii=False),
                encoding="utf-8",
            )

            code = cli_main([
                "--brief-file", str(brief_path),
                "--output", str(output),
                "--source-root", str(root),
                "--vision-analysis-json", str(vision_path),
                "--loop",
                "--max-loops", "3",
            ])

            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "out.loop1.xlsx").exists())
            self.assertTrue((Path(tmp) / "out.loop1.audit.json").exists())

    def test_cli_auto_pipeline_manual_assets_override_cache_and_write_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            brief_path = Path(tmp) / "brief.txt"
            brief_path.write_text(FIXED_REAL_BRIEF_TEXT, encoding="utf-8")
            write_full_launch_media(root)
            media_dir = root / "发布会类" / "美的智美万象发布会"
            workbook_path = root / "M+发布会类通用脚本库.xlsx"
            write_launch_library(workbook_path, full_launch_template_rows(), [])
            refresh_ai_spec(workbook_path)
            manifest = build_image_manifest(media_dir, project_hint="美的智美万象发布会")
            manifest_path = Path(tmp) / "manual_manifest.jsonl"
            analysis_path = Path(tmp) / "manual_analysis.jsonl"
            write_jsonl(manifest_path, manifest)
            write_jsonl(analysis_path, [
                {
                    "image_id": row["image_id"],
                    "caption": full_launch_visual_evidence(index)[0],
                    "tags": full_launch_visual_evidence(index)[1],
                    "brand_names": ["美的"],
                    "source": "minimax",
                    "analysis_provider": "minimax",
                    "analysis_model": "mmx-vision-describe/provider-managed",
                    "provenance_status": "api_generated",
                }
                for index, row in enumerate(manifest, start=1)
            ])
            output = Path(tmp) / "out.xlsx"
            cache_root = Path(tmp) / "assets"

            code = cli_main([
                "--brief-file", str(brief_path),
                "--output", str(output),
                "--source-root", str(root),
                "--source-view", "ai-spec",
                "--auto-pipeline",
                "--asset-cache-root", str(cache_root),
                "--image-manifest-jsonl", str(manifest_path),
                "--image-analysis-jsonl", str(analysis_path),
            ])

            cache_dir = project_cache_dir(read_text_brief(FIXED_REAL_BRIEF_TEXT), cache_root)
            self.assertFalse((cache_dir / "CURRENT").exists())
            report_path = next((cache_dir / "runs").glob("*/run_report.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(code, 3)
            self.assertTrue(Path(report["output_path"]).exists())
            self.assertTrue(Path(report["visual_review_html"]).exists())
            self.assertEqual(report["visual_analysis_coverage"], 1.0)
            self.assertEqual(report["generated_analysis_count"], 0)
            self.assertEqual(report["reused_analysis_count"], 18)
            self.assertEqual(report["machine_audit_status"], "pass")
            self.assertEqual(report["machine_audit_stage_status"], "succeeded_with_warnings")
            self.assertEqual(report["human_review_status"], "pending")
            self.assertEqual(report["release_decision"], "blocked")
            self.assertEqual(report["generation_exit_code"], 0)
            self.assertEqual(report["status"], "waiting_human_review")
            self.assertEqual(report["exit_code"], 3)
            self.assertGreater(report["pending_human_review_count"], 0)

    def test_cli_auto_pipeline_requires_manual_manifest_analysis_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief_path = Path(tmp) / "brief.txt"
            brief_path.write_text(FIXED_REAL_BRIEF_TEXT, encoding="utf-8")
            manifest = Path(tmp) / "manifest.jsonl"
            write_jsonl(manifest, [{"image_id": "img_1", "原图路径": "/tmp/01.png"}])

            code = cli_main([
                "--brief-file", str(brief_path),
                "--output", str(Path(tmp) / "out.xlsx"),
                "--auto-pipeline",
                "--image-manifest-jsonl", str(manifest),
            ])

            self.assertEqual(code, 1)

    def test_generation_loop_writes_visual_asset_sheet_and_passes_evidence_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            write_full_launch_media(root)
            write_launch_library(root / "M+发布会类通用脚本库.xlsx", full_launch_template_rows(), [])
            output = Path(tmp) / "out.xlsx"
            brief = read_text_brief(FIXED_REAL_BRIEF_TEXT)
            analysis = full_launch_vision_analysis()
            analysis["01.png"] = {
                "summary": "Midea logo 特写画面",
                "tags": ["logo", "品牌标识", "Midea"],
                "brand_names": ["Midea"],
                "source": "人工复核",
            }

            result = run_generation_loop([brief], root, output, max_loops=3, vision_analysis=analysis)
            wb = load_workbook(output)

            self.assertEqual(result.exit_code, 0)
            self.assertIn("视觉资产", wb.sheetnames)
            ws = wb["视觉资产"]
            headers = [ws.cell(1, col).value for col in range(1, ws.max_column + 1)]
            row = {headers[index]: ws.cell(2, index + 1).value for index in range(len(headers))}
            self.assertEqual(row["原图宽"], 1920)
            self.assertEqual(row["视觉读取状态"], "人工复核")
            self.assertEqual(row["视觉内容摘要"], "Midea logo 特写画面")
            self.assertEqual(len(ws._images), 0)
            self.assertFalse([
                check for check in result.iterations[-1].audit["checks"]
                if check["检查项"] == "missing_visual_asset"
            ])
            wb.close()

    def test_real_launch_smoke_run_reads_brief_scores_and_writes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            anonymous_launch_template(source_root)
            briefs = [read_text_brief(FIXED_REAL_BRIEF_TEXT)]
            shots = load_launch_source_shots(source_root)
            scored = score_candidates(briefs[0], shots)
            rows = generate_script_rows(briefs[0], scored)
            output = Path(tmp) / "real_output.xlsx"

            write_output_workbook(output, rows)

            self.assertTrue(scored)
            self.assertTrue(all(row["来源文件"] for row in rows))
            wb = load_workbook(output, read_only=True)
            self.assertEqual(wb["脚本"].max_row, len(rows) + 1)
            wb.close()

    def test_real_template_driven_smoke_run_preserves_template_slot_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            template_path = anonymous_launch_template(source_root)
            brief = read_text_brief(FIXED_REAL_BRIEF_TEXT)
            project_hint = project_hint_from_brief(brief)
            slots = load_template_slots(source_root, project_hint=project_hint)
            variants = load_creative_variants(source_root, project_hint=project_hint)
            decisions = match_template_slots(brief, slots, variants)
            script_rows, detail_rows = generate_template_rows(brief, decisions)
            output = Path(tmp) / "template_output.xlsx"

            write_template_output_workbook(
                output,
                script_rows,
                detail_rows,
                template_workbook_path=template_path,
            )

            self.assertEqual(len(script_rows), len(slots))
            self.assertTrue(all(
                row["采用来源文件"] and row["采用来源 sheet"] and row["采用来源行"]
                for row in detail_rows
            ))
            self.assertTrue(any(row["画面示意"] for row in script_rows))
            self.assertFalse(any("需结合 Brief" in str(row["画面描述"]) for row in script_rows))
            self.assertFalse(any("承接模板" in str(row["画面描述"]) for row in script_rows))
            duplicate_sources = [
                item for item, count in Counter(
                    (row["替换来源 sheet"], row["替换来源行"])
                    for row in detail_rows
                    if row["替换来源行"]
                ).items()
                if count > 2
            ]
            self.assertEqual(duplicate_sources, [])
            wb = load_workbook(output)
            self.assertEqual(
                wb.sheetnames,
                ["脚本", "抽调明细", "剪辑复核", "相邻镜头复核", "整片复核"],
            )
            self.assertEqual(wb["脚本"].max_row, len(slots) + 6)
            self.assertIn("A1:K1", [str(item) for item in wb["脚本"].merged_cells.ranges])
            self.assertGreaterEqual(wb["脚本"].row_dimensions[7].height, 120)
            self.assertEqual(len(wb["脚本"]._images), len(script_rows))
            wb.close()


if __name__ == "__main__":
    unittest.main()
