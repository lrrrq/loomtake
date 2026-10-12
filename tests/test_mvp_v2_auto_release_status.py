import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from PIL import Image

from mvp_v2.cli import main as cli_main
from mvp_v2.loop_runner import LoopIteration, LoopResult
from mvp_v2.review_quality import EDITORIAL_REVIEW_COLUMNS
from mvp_v2.run_contract import sha256_file
from mvp_v2.stage_contract import StageStatus
from mvp_v2.stage_runtime import finish_pipeline_stage, start_pipeline_stage


BRIEF_TEXT = """美的智美万象发布会开场短片

脚本类型：发布会
项目区域文件名：家电-发布会-【美的智美万象发布会】分镜脚本.xlsx
品牌或客户：美的
行业或活动类别：家电/科技发布会
视频时长：60s
风格调性：硬核科技感
必须出现的内容：品牌logo、活动主会场
禁忌或不能出现的内容：不要低质感口号堆砌
素材目录或素材文件：/tmp/发布会类
备注：测试
"""


class AutoReleaseStatusTests(unittest.TestCase):
    def test_auto_pipeline_missing_media_reaches_human_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path = root / "brief.txt"
            brief_path.write_text(BRIEF_TEXT, encoding="utf-8")
            cache_root = root / "assets"
            _write_source_workbook(root / "missing-source")

            with patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=_successful_blank_generation,
            ):
                code = cli_main([
                    "--brief-file", str(brief_path),
                    "--output", str(root / "output.xlsx"),
                    "--source-root", str(root / "missing-source"),
                    "--auto-pipeline",
                    "--asset-cache-root", str(cache_root),
                ])

            report_path = next((cache_root / "美的智美万象发布会" / "runs").glob("*/run_report.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(code, 3)
            self.assertEqual(report["machine_audit_status"], "pass")
            self.assertEqual(report["human_review_status"], "pending")
            self.assertEqual(report["status"], "waiting_human_review")
            self.assertEqual(report["script_type"], "发布会")
            self.assertEqual(report["category_id"], "launch")
            self.assertEqual(
                report["route_contract"]["planning_profile"],
                "launch_slot_contract",
            )
            self.assertEqual(report["media_readiness_status"], "not_ready")
            self.assertFalse((cache_root / "美的智美万象发布会" / "CURRENT").exists())

    def test_auto_pipeline_missing_api_key_reaches_human_review_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path = root / "brief.txt"
            brief_path.write_text(BRIEF_TEXT, encoding="utf-8")
            media_dir = root / "source" / "发布会类" / "美的智美万象发布会"
            media_dir.mkdir(parents=True)
            Image.new("RGB", (32, 32), color="white").save(media_dir / "image.png")
            cache_root = root / "assets"
            _write_source_workbook(root / "source")

            with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=False), patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=_successful_blank_generation,
            ):
                code = cli_main([
                    "--brief-file", str(brief_path),
                    "--output", str(root / "output.xlsx"),
                    "--source-root", str(root / "source"),
                    "--auto-pipeline",
                    "--asset-cache-root", str(cache_root),
                ])

            report_path = next((cache_root / "美的智美万象发布会" / "runs").glob("*/run_report.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(code, 3)
            self.assertEqual(report["machine_audit_status"], "pass")
            self.assertEqual(report["human_review_status"], "pending")
            self.assertEqual(report["media_readiness_status"], "not_ready")

    def test_auto_pipeline_blocks_blank_editorial_review_but_keeps_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path = root / "brief.txt"
            brief_path.write_text(BRIEF_TEXT, encoding="utf-8")
            manifest_path = root / "manifest.jsonl"
            analysis_path = root / "analysis.jsonl"
            manifest_path.write_text('{"image_id": "img_1"}\n', encoding="utf-8")
            analysis_path.write_text(
                '{"image_id": "img_1", "caption": "美的 logo", "tags": ["美的"]}\n',
                encoding="utf-8",
            )
            output_path = root / "output.xlsx"
            cache_root = root / "assets"
            _write_source_workbook(root / "source")

            def fake_generation_loop(briefs, source_root, output, **kwargs):
                workbook_path = Path(output)
                audit_path = workbook_path.with_suffix(".audit.json")
                _write_blank_review_workbook(workbook_path)
                audit = {
                    "status": "pass",
                    "hard_fail_count": 0,
                    "warning_count": 0,
                    "score": 100,
                    "blocking_items": [],
                }
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                log_path = workbook_path.with_suffix(".loop.json")
                log_path.write_text('{"iteration":1}\n', encoding="utf-8")
                _complete_generation_stages(kwargs["runtime"], workbook_path, audit_path)
                iteration = LoopIteration(1, workbook_path, audit_path, audit)
                return LoopResult(
                    exit_code=0,
                    output_path=workbook_path,
                    audit_path=audit_path,
                    log_path=log_path,
                    iterations=[iteration],
                    passed_iteration=1,
                )

            with patch("mvp_v2.cli.run_generation_loop", side_effect=fake_generation_loop):
                code = cli_main([
                    "--brief-file", str(brief_path),
                    "--output", str(output_path),
                    "--source-root", str(root / "source"),
                    "--auto-pipeline",
                    "--asset-cache-root", str(cache_root),
                    "--image-manifest-jsonl", str(manifest_path),
                    "--image-analysis-jsonl", str(analysis_path),
                ])

            report = _read_report(cache_root)
            self.assertEqual(code, 3)
            self.assertEqual(report["machine_audit_status"], "pass")
            self.assertEqual(report["human_review_status"], "pending")
            self.assertEqual(report["pending_human_review_count"], 18)
            self.assertEqual(report["release_decision"], "blocked")
            self.assertEqual(report["status"], "waiting_human_review")
            self.assertEqual(report["generation_exit_code"], 0)
            self.assertEqual(report["exit_code"], 3)
            self.assertFalse(
                (cache_root / "美的智美万象发布会" / "CURRENT").exists()
            )
            self.assertTrue(Path(report["output_path"]).exists())
            self.assertTrue(Path(report["audit_path"]).exists())
            self.assertTrue(Path(report["visual_review_html"]).exists())
            self.assertEqual(report["manifest_sha256"], sha256_file(manifest_path))
            self.assertEqual(report["analysis_sha256"], sha256_file(analysis_path))

            manifest_path.write_text('{"changed":true}\n', encoding="utf-8")
            analysis_path.write_text('{"changed":true}\n', encoding="utf-8")
            persisted_report = _read_report(cache_root)
            self.assertEqual(persisted_report["manifest_sha256"], report["manifest_sha256"])
            self.assertEqual(persisted_report["analysis_sha256"], report["analysis_sha256"])

    def test_auto_pipeline_machine_failure_uses_best_failed_audit_metrics_and_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = _write_manual_pipeline_inputs(root)
            output_path = root / "output.xlsx"
            cache_root = root / "assets"
            best_output_path = root / "output.best_failed.xlsx"
            best_audit_path = root / "output.best_failed.audit.json"
            final_audit_path = root / "output.loop2.audit.json"
            _write_blank_review_workbook(best_output_path)
            best_audit = {
                "status": "fail",
                "hard_fail_count": 1,
                "warning_count": 0,
                "score": 88,
                "blocking_items": ["best-failure"],
            }
            final_audit = {
                "status": "fail",
                "hard_fail_count": 9,
                "warning_count": 4,
                "score": 1,
                "blocking_items": ["last-failure"],
            }
            best_audit_path.write_text(json.dumps(best_audit), encoding="utf-8")
            final_audit_path.write_text(json.dumps(final_audit), encoding="utf-8")
            loop_log_path = root / "output.loop.json"
            loop_log_path.write_text('{"iterations":2}\n', encoding="utf-8")

            def fake_generation_loop(*args, **kwargs):
                return LoopResult(
                    exit_code=2,
                    output_path=output_path,
                    audit_path=final_audit_path,
                    log_path=loop_log_path,
                    iterations=[
                        LoopIteration(1, best_output_path, best_audit_path, best_audit),
                        LoopIteration(2, output_path, final_audit_path, final_audit),
                    ],
                    best_failed_output_path=best_output_path,
                    best_failed_audit_path=best_audit_path,
                )

            with patch("mvp_v2.cli.run_generation_loop", side_effect=fake_generation_loop):
                code = cli_main(_auto_pipeline_args(
                    brief_path, output_path, root / "source", cache_root, manifest_path, analysis_path
                ))

            report = _read_report(cache_root)
            self.assertEqual(code, 2)
            self.assertEqual(report["machine_audit_status"], "fail")
            self.assertEqual(report["generation_exit_code"], 2)
            self.assertEqual(report["exit_code"], 2)
            self.assertEqual(Path(report["audit_path"]).name, "output.best_failed.audit.json")
            self.assertEqual(report["hard_fail_count"], 1)
            self.assertEqual(report["warning_count"], 0)
            self.assertEqual(report["score"], 88)
            self.assertEqual(report["blocking_items"], ["best-failure"])

    def test_auto_pipeline_machine_warning_is_release_blocked_with_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = _write_manual_pipeline_inputs(root)
            output_path = root / "output.xlsx"
            generated_path = root / "generated.xlsx"
            audit_path = generated_path.with_suffix(".audit.json")
            cache_root = root / "assets"
            _write_blank_review_workbook(generated_path)
            audit = {
                "status": "pass",
                "hard_fail_count": 0,
                "warning_count": 1,
                "score": 95,
                "blocking_items": [],
            }
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            loop_log_path = root / "generated.loop.json"
            loop_log_path.write_text('{"iteration":1}\n', encoding="utf-8")

            def fake_generation_loop(*args, **kwargs):
                _complete_generation_stages(kwargs["runtime"], generated_path, audit_path)
                return LoopResult(
                    exit_code=0,
                    output_path=generated_path,
                    audit_path=audit_path,
                    log_path=loop_log_path,
                    iterations=[LoopIteration(1, output_path, audit_path, audit)],
                    passed_iteration=1,
                )

            with patch("mvp_v2.cli.run_generation_loop", side_effect=fake_generation_loop):
                code = cli_main(_auto_pipeline_args(
                    brief_path, output_path, root / "source", cache_root, manifest_path, analysis_path
                ))

            report = _read_report(cache_root)
            self.assertEqual(code, 3)
            self.assertEqual(report["machine_audit_status"], "pass")
            self.assertEqual(report["machine_audit_stage_status"], "succeeded_with_warnings")
            self.assertEqual(report["human_review_status"], "pending")
            self.assertEqual(report["generation_exit_code"], 0)
            self.assertEqual(report["exit_code"], 3)
            self.assertTrue(Path(report["output_path"]).exists())
            self.assertTrue(Path(report["audit_path"]).exists())
            self.assertTrue(Path(report["visual_review_html"]).exists())

    def test_auto_pipeline_invalid_human_review_is_release_blocked_with_exit_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = _write_manual_pipeline_inputs(root)
            output_path = root / "output.xlsx"
            generated_path = root / "generated.xlsx"
            audit_path = generated_path.with_suffix(".audit.json")
            cache_root = root / "assets"
            _write_invalid_review_workbook(generated_path)
            audit = {
                "status": "pass",
                "hard_fail_count": 0,
                "warning_count": 0,
                "score": 100,
                "blocking_items": [],
            }
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            loop_log_path = root / "generated.loop.json"
            loop_log_path.write_text('{"iteration":1}\n', encoding="utf-8")

            def fake_generation_loop(*args, **kwargs):
                _complete_generation_stages(kwargs["runtime"], generated_path, audit_path)
                return LoopResult(
                    exit_code=0,
                    output_path=generated_path,
                    audit_path=audit_path,
                    log_path=loop_log_path,
                    iterations=[LoopIteration(1, output_path, audit_path, audit)],
                    passed_iteration=1,
                )

            with patch("mvp_v2.cli.run_generation_loop", side_effect=fake_generation_loop):
                code = cli_main(_auto_pipeline_args(
                    brief_path, output_path, root / "source", cache_root, manifest_path, analysis_path
                ))

            report = _read_report(cache_root)
            self.assertEqual(code, 3)
            self.assertEqual(report["machine_audit_status"], "pass")
            self.assertEqual(report["human_review_status"], "invalid")
            self.assertEqual(report["release_decision"], "blocked")
            self.assertEqual(report["generation_exit_code"], 0)
            self.assertEqual(report["exit_code"], 3)
            self.assertTrue(Path(report["output_path"]).exists())
            self.assertTrue(Path(report["audit_path"]).exists())
            self.assertTrue(Path(report["visual_review_html"]).exists())


def _write_blank_review_workbook(path: Path) -> None:
    workbook = Workbook()
    details = workbook.active
    details.title = "抽调明细"
    details.append(["镜号"])
    visuals = workbook.create_sheet("视觉资产")
    visuals.append(["镜号", "配图判断"])
    review = workbook.create_sheet("剪辑复核")
    review.append(EDITORIAL_REVIEW_COLUMNS)
    for slot in range(1, 19):
        row = {column: "" for column in EDITORIAL_REVIEW_COLUMNS}
        row.update({"项目名称": "美的智美万象发布会", "slot_index": slot, "镜号": slot})
        review.append([row[column] for column in EDITORIAL_REVIEW_COLUMNS])
    workbook.save(path)


def _successful_blank_generation(*args, **kwargs):
    workbook_path = Path(args[2])
    audit_path = workbook_path.with_suffix(".audit.json")
    _write_blank_review_workbook(workbook_path)
    audit = {
        "status": "pass",
        "hard_fail_count": 0,
        "warning_count": 0,
        "score": 100,
        "blocking_items": [],
        "script_document_audit": {
            "status": "pass",
            "hard_fail_count": 0,
            "warning_count": 0,
        },
        "media_readiness": {"status": "not_assessed", "warning_count": 0},
    }
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    log_path = workbook_path.with_suffix(".loop.json")
    log_path.write_text('{"iteration":1}\n', encoding="utf-8")
    _complete_generation_stages(kwargs["runtime"], workbook_path, audit_path)
    iteration = LoopIteration(1, workbook_path, audit_path, audit)
    return LoopResult(
        exit_code=0,
        output_path=workbook_path,
        audit_path=audit_path,
        log_path=log_path,
        iterations=[iteration],
        passed_iteration=1,
    )


def _write_invalid_review_workbook(path: Path) -> None:
    workbook = Workbook()
    details = workbook.active
    details.title = "抽调明细"
    details.append(["镜号"])
    visuals = workbook.create_sheet("视觉资产")
    visuals.append(["镜号", "配图判断"])
    workbook.save(path)


def _write_manual_pipeline_inputs(root: Path) -> tuple[Path, Path, Path]:
    brief_path = root / "brief.txt"
    brief_path.write_text(BRIEF_TEXT, encoding="utf-8")
    manifest_path = root / "manifest.jsonl"
    analysis_path = root / "analysis.jsonl"
    manifest_path.write_text('{"image_id": "img_1"}\n', encoding="utf-8")
    analysis_path.write_text(
        '{"image_id": "img_1", "caption": "美的 logo", "tags": ["美的"]}\n',
        encoding="utf-8",
    )
    _write_source_workbook(root / "source")
    return brief_path, manifest_path, analysis_path


def _write_source_workbook(source_root: Path) -> None:
    source_root.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    for column, header in enumerate(
        ("镜号", "结构", "时长(秒)", "画面描述", "声音"),
        start=1,
    ):
        template.cell(6, column, header)
    for column, value in enumerate(
        ("1", "开场", "60", "匿名发布会画面", "音乐"),
        start=1,
    ):
        template.cell(7, column, value)
    workbook.create_sheet("创意库")
    workbook.save(source_root / "M+发布会类通用脚本库.xlsx")
    workbook.close()


def _auto_pipeline_args(
    brief_path: Path,
    output_path: Path,
    source_root: Path,
    cache_root: Path,
    manifest_path: Path,
    analysis_path: Path,
) -> list[str]:
    return [
        "--brief-file", str(brief_path),
        "--output", str(output_path),
        "--source-root", str(source_root),
        "--auto-pipeline",
        "--asset-cache-root", str(cache_root),
        "--image-manifest-jsonl", str(manifest_path),
        "--image-analysis-jsonl", str(analysis_path),
    ]


def _read_report(cache_root: Path) -> dict:
    cache_dir = cache_root / "美的智美万象发布会"
    path = max(
        (cache_dir / "runs").glob("*/run_report.json"),
        key=lambda candidate: candidate.stat().st_mtime_ns,
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _complete_generation_stages(runtime, workbook: Path, audit: Path) -> None:
    for stage_id in (
        "script_plan",
        "script_draft",
        "visual_binding",
        "workbook_render",
        "machine_audit",
    ):
        output = (
            workbook
            if stage_id in {"workbook_render", "machine_audit"}
            else runtime.run_dir / f"{stage_id}.json"
        )
        if not output.exists():
            output.write_text(stage_id, encoding="utf-8")
        start_pipeline_stage(
            runtime,
            stage_id,
            input_sha256=f"input-{stage_id}",
            input_refs=(),
        )
        refs = (
            (str(workbook.resolve()), str(audit.resolve()))
            if stage_id == "machine_audit"
            else (str(output.resolve()),)
        )
        finish_pipeline_stage(
            runtime,
            stage_id,
            StageStatus.SUCCEEDED,
            output_sha256=f"output-{stage_id}",
            output_refs=refs,
        )


def _assert_error_report_schema(test_case: unittest.TestCase, report: dict) -> None:
    required_keys = {
        "project_name", "project_hint", "machine_audit_status", "human_review_status",
        "release_decision", "generation_exit_code", "status", "exit_code", "output_path",
        "audit_path", "visual_review_html", "manifest_path", "analysis_path", "manifest_count",
        "analysis_count", "visual_analysis_coverage", "reused_analysis_count",
        "generated_analysis_count", "loop_count", "passed_iteration", "hard_fail_count",
        "warning_count", "score", "blocking_items", "pending_human_review_count", "error",
    }
    test_case.assertTrue(required_keys.issubset(report))
    test_case.assertEqual(report["machine_audit_status"], "fail")
    test_case.assertEqual(report["human_review_status"], "invalid")
    test_case.assertEqual(report["release_decision"], "blocked")
    test_case.assertEqual(report["generation_exit_code"], 1)
    test_case.assertEqual(report["status"], "blocked")
    test_case.assertEqual(report["exit_code"], 1)
