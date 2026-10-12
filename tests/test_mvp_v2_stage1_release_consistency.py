import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from PIL import Image

from mvp_v2.ai_spec import refresh_ai_spec
from mvp_v2.auto_pipeline import project_cache_dir
from mvp_v2.cli import main as cli_main
from mvp_v2.excel_io import read_text_brief, write_template_output_workbook
from mvp_v2.image_assets import build_image_manifest, write_jsonl
from mvp_v2.loop_runner import LoopIteration, LoopResult
from mvp_v2.pipeline_runtime import resolve_pipeline_run
from mvp_v2.run_contract import sha256_file
from mvp_v2.stage_contract import (
    StageError,
    StageErrorCategory,
    StageStatus,
)
from mvp_v2.stage_runtime import finish_pipeline_stage, start_pipeline_stage


BRIEF_TEXT = """Stage1 一致性发布会

脚本类型：发布会
项目区域文件名：一致性发布会.xlsx
品牌或客户：一致性品牌
行业或活动类别：科技发布会
视频时长：60s
风格调性：科技感、稳重大气
必须出现的内容：品牌logo、活动主会场、主题装置空镜、人流互动、核心发布仪式
禁忌或不能出现的内容：竞品品牌
素材目录或素材文件：一致性发布会
备注：Stage1 组合一致性测试
"""


class Stage1ReleaseConsistencyE2ETests(unittest.TestCase):
    def test_machine_failed_run_keeps_old_published_bundle_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = _write_manual_inputs(root)
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            calls = 0

            def controlled_loop(briefs, source_root, run_output, **kwargs):
                nonlocal calls
                calls += 1
                run_output = Path(run_output)
                marker = "old-machine-pass" if calls == 1 else "new-machine-fail"
                audit = {
                    "status": "pass" if calls == 1 else "fail",
                    "hard_fail_count": 0 if calls == 1 else 1,
                    "warning_count": 0,
                    "score": 100 if calls == 1 else 70,
                    "blocking_items": [] if calls == 1 else [marker],
                }
                if calls == 1:
                    workbook_path = run_output
                    audit_path = Path(str(run_output) + ".audit.json")
                    _write_pending_workbook(workbook_path, marker)
                    audit_path.write_text(json.dumps(audit), encoding="utf-8")
                    log_path = Path(str(run_output) + ".loop.json")
                    log_path.write_text(json.dumps({"marker": marker}), encoding="utf-8")
                    _complete_generation_stages(
                        kwargs["runtime"],
                        workbook_path,
                        audit_path,
                        machine_pass=True,
                    )
                    return LoopResult(
                        exit_code=0,
                        output_path=workbook_path,
                        audit_path=audit_path,
                        log_path=log_path,
                        iterations=[LoopIteration(1, workbook_path, audit_path, audit)],
                        passed_iteration=1,
                    )

                workbook_path = run_output.with_name(f"{run_output.stem}.best_failed{run_output.suffix}")
                audit_path = run_output.with_name(f"{run_output.stem}.best_failed.audit.json")
                _write_pending_workbook(workbook_path, marker)
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                log_path = Path(str(run_output) + ".loop.json")
                log_path.write_text(json.dumps({"marker": marker}), encoding="utf-8")
                _complete_generation_stages(
                    kwargs["runtime"],
                    workbook_path,
                    audit_path,
                    machine_pass=False,
                )
                return LoopResult(
                    exit_code=2,
                    output_path=run_output,
                    audit_path=Path(str(run_output) + ".audit.json"),
                    log_path=log_path,
                    iterations=[LoopIteration(1, workbook_path, audit_path, audit)],
                    best_failed_output_path=workbook_path,
                    best_failed_audit_path=audit_path,
                )

            def fake_review(path, workbook_path):
                target = Path(path)
                target.write_text(f"review:{Path(workbook_path).name}", encoding="utf-8")
                return target

            args = _auto_pipeline_args(
                brief_path, output, root / "source", cache_root, manifest_path, analysis_path
            )
            with patch("mvp_v2.cli.run_generation_loop", side_effect=controlled_loop), patch(
                "mvp_v2.cli._write_review_html_if_requested", side_effect=fake_review
            ), patch(
                "mvp_v2.review_publish_stages.analyze_review_workbooks",
                return_value=_quality_pass_report(),
            ):
                old_exit = cli_main(args)
                cache_dir = project_cache_dir(read_text_brief(BRIEF_TEXT), cache_root)
                old_run_dir = next((cache_dir / "runs").iterdir())
                old_snapshot = resolve_pipeline_run(cache_dir, old_run_dir.name, allowed_statuses={"succeeded"})
                old_run_dir = next(iter(old_snapshot.values())).parent
                old_hashes = {name: sha256_file(path) for name, path in old_snapshot.items()}
                old_report = _read_json(old_snapshot["run_report"])

                new_exit = cli_main(args)

            current_snapshot = old_snapshot
            new_run_dir = next(
                path
                for path in (cache_dir / "runs").iterdir()
                if path.resolve() != old_run_dir.resolve()
            )
            new_manifest = _read_json(new_run_dir / "run_manifest.json")
            new_report = _read_json(new_run_dir / "run_report.json")
            new_audit = _read_json(Path(new_report["audit_path"]))

            self.assertEqual(old_exit, 0)
            self.assertEqual(old_report["machine_audit_status"], "pass")
            self.assertEqual(old_report["human_review_status"], "quality_pass")
            self.assertEqual(new_exit, 2)
            self.assertNotEqual(new_run_dir, old_run_dir)
            self.assertFalse((cache_dir / "CURRENT").exists())
            self.assertEqual(new_manifest["status"], "blocked")
            self.assertNotIn("bundle_finalized", new_manifest)
            self.assertEqual(new_report["machine_audit_status"], "fail")
            self.assertEqual(new_report["run_id"], new_run_dir.name)
            self.assertEqual(new_manifest["run_id"], new_run_dir.name)
            self.assertEqual(new_audit["blocking_items"], ["new-machine-fail"])
            self.assertEqual(
                _workbook_marker(Path(new_report["output_path"])),
                "new-machine-fail",
            )
            self.assertEqual(
                new_manifest["stages"]["human_review"]["status"],
                "not_started",
            )
            self.assertEqual(
                new_manifest["stages"]["publish_feedback"]["status"],
                "not_started",
            )

            self.assertEqual(
                {name: sha256_file(path) for name, path in current_snapshot.items()},
                old_hashes,
            )
            self.assertEqual(
                _workbook_marker(current_snapshot["workbook"]),
                "old-machine-pass",
            )
            self.assertTrue(all(path.is_file() for path in current_snapshot.values()))

    def test_real_auto_pipeline_keeps_ledger_manifest_report_current_and_hashes_on_one_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            source_root.mkdir()
            brief_path = root / "brief.txt"
            brief_path.write_text(BRIEF_TEXT, encoding="utf-8")
            workbook_path = source_root / "M+发布会类通用脚本库.xlsx"
            _write_launch_library(workbook_path)
            refresh_ai_spec(workbook_path)
            media_dir = source_root / "发布会类" / "一致性发布会"
            _write_launch_images(media_dir)
            manifest_rows = build_image_manifest(media_dir, project_hint="一致性发布会")
            manifest_path = root / "manual_manifest.jsonl"
            analysis_path = root / "manual_analysis.jsonl"
            write_jsonl(manifest_path, manifest_rows)
            write_jsonl(analysis_path, _trusted_analysis(manifest_rows))
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"

            code = cli_main(_auto_pipeline_args(
                brief_path, output, source_root, cache_root, manifest_path, analysis_path
            ))

            cache_dir = project_cache_dir(read_text_brief(BRIEF_TEXT), cache_root)
            self.assertFalse((cache_dir / "CURRENT").exists())
            run_dir = next((cache_dir / "runs").iterdir())
            manifest = _read_json(run_dir / "run_manifest.json")
            report = _read_json(run_dir / "run_report.json")
            release = {
                name: Path(record["path"]).resolve()
                for name, record in manifest["artifacts"].items()
            }
            ledger_rows = [
                json.loads(line)
                for line in (cache_dir / "run_ledger.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            run_rows = [row for row in ledger_rows if row["run_id"] == run_dir.name]

            self.assertIn(code, {2, 3})
            self.assertEqual(manifest["run_id"], run_dir.name)
            self.assertEqual(report["run_id"], run_dir.name)
            self.assertEqual(Path(report["run_dir"]).resolve(), run_dir.resolve())
            self.assertEqual(
                [row["event"] for row in run_rows],
                ["started", manifest["status"]],
            )
            self.assertIn(manifest["status"], {"blocked", "waiting_human_review"})
            self.assertIn(report["status"], {"blocked", "waiting_human_review"})
            self.assertEqual(report["release_decision"], "blocked")
            self.assertEqual(set(manifest["artifacts"]), set(release))
            self.assertEqual(
                {name: Path(record["path"]).resolve() for name, record in manifest["artifacts"].items()},
                release,
            )
            for name, path in release.items():
                record = manifest["artifacts"][name]
                self.assertEqual(path.parent.resolve(), run_dir.resolve())
                self.assertEqual(record["sha256"], sha256_file(path))
                self.assertEqual(record["size_bytes"], path.stat().st_size)
            self.assertEqual(Path(report["output_path"]).resolve(), release["workbook"])
            self.assertEqual(Path(report["audit_path"]).resolve(), release["audit"])
            self.assertEqual(Path(report["visual_review_html"]).resolve(), release["visual_review"])
            self.assertEqual(
                Path(report["run_manifest_path"]).resolve(),
                (run_dir / "run_manifest.json").resolve(),
            )
            self.assertEqual(
                Path(report["run_ledger_path"]).resolve(),
                (cache_dir / "run_ledger.jsonl").resolve(),
            )
            self.assertEqual(report["output_sha256"], sha256_file(release["workbook"]))


def _auto_pipeline_args(brief, output, source_root, cache_root, manifest, analysis):
    return [
        "--brief-file", str(brief),
        "--output", str(output),
        "--source-root", str(source_root),
        "--source-view", "ai-spec",
        "--asset-cache-root", str(cache_root),
        "--auto-pipeline",
        "--image-manifest-jsonl", str(manifest),
        "--image-analysis-jsonl", str(analysis),
        "--max-loops", "2",
    ]


def _write_manual_inputs(root: Path):
    brief_path = root / "brief.txt"
    brief_path.write_text(BRIEF_TEXT, encoding="utf-8")
    manifest_path = root / "manifest.jsonl"
    analysis_path = root / "analysis.jsonl"
    write_jsonl(manifest_path, [{"image_id": "manual-1", "原图路径": "/tmp/manual-1.png"}])
    write_jsonl(analysis_path, [{
        "image_id": "manual-1",
        "caption": "一致性品牌发布会 logo",
        "tags": ["一致性品牌", "发布会", "logo"],
        "source": "minimax",
    }])
    source_root = root / "source"
    source_root.mkdir()
    workbook_path = source_root / "M+发布会类通用脚本库.xlsx"
    _write_launch_library(workbook_path)
    refresh_ai_spec(workbook_path)
    return brief_path, manifest_path, analysis_path


def _write_pending_workbook(path: Path, marker: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    script_rows = [
        {
            "结构": _structure(index),
            "镜号": str(index),
            "时长(秒)": 3 if index < 18 else 9,
            "画面描述": marker if index == 1 else f"{marker}-镜头{index}",
            "声音": "音乐",
        }
        for index in range(1, 19)
    ]
    detail_rows = [
        {"项目名称": marker, "slot_index": index, "模板镜号": str(index)}
        for index in range(1, 19)
    ]
    write_template_output_workbook(path, script_rows, detail_rows)


def _write_launch_library(path: Path):
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    headers = [
        ["M+发布会类通用模版"],
        ["整体说明", "Stage1 consistency"],
        [],
        [],
        ["结构", "镜号", "内容"],
        [None, None, "画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注"],
    ]
    for row in headers:
        template.append(row)
    for index in range(1, 19):
        template.append([
            _structure(index), index, None, None, "字体穿梭", "航拍", "全景", "固定",
            _description(index), "音乐", "",
        ])
    creative = workbook.create_sheet("创意库")
    for row in [
        ["M+发布会类通用脚本库"],
        ["整体说明", "Stage1 consistency"],
        [],
        [],
        ["镜号", "结构", "形式", "内容"],
        [None, None, None, "画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"],
    ]:
        creative.append(row)
    workbook.save(path)
    workbook.close()


def _write_launch_images(media_dir: Path):
    media_dir.mkdir(parents=True)
    for index in range(1, 19):
        Image.new("RGB", (640, 360), color=(index * 10 % 255, 80, 160)).save(
            media_dir / f"{index:02d}.png"
        )


def _trusted_analysis(manifest_rows):
    concepts = ["品牌logo", "活动主会场", "主题装置空镜", "人流互动", "核心发布仪式"]
    return [
        {
            "image_id": row["image_id"],
            "caption": f"清晰的一致性品牌科技发布会现场，{'、'.join(concepts)}，第{index}镜",
            "tags": ["一致性品牌", "发布会", "logo", "主会场", "主题装置", "空镜", "人流", "互动", "核心发布仪式"],
            "brand_names": ["一致性品牌"],
            "source": "minimax",
            "analysis_provider": "minimax",
            "analysis_model": "mmx-vision-describe/provider-managed",
            "provenance_status": "api_generated",
        }
        for index, row in enumerate(manifest_rows, start=1)
    ]


def _structure(index: int) -> str:
    if index <= 2:
        return "开场造势"
    if 5 <= index <= 9:
        return "人气与现场互动"
    if index >= 17:
        return "收束"
    return "核心宣讲与仪式"


def _description(index: int) -> str:
    return {
        1: "一致性品牌logo穿梭入画",
        2: "活动主会场全景建立空间",
        4: "主题装置空镜展示空间设计",
        5: "嘉宾人流互动与观众交流",
        12: "核心发布仪式形成高潮",
        18: "一致性品牌主题字样与品牌logo收束",
    }.get(index, "发布会现场细节镜头")


def _workbook_marker(path: Path) -> str:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        return str(workbook["脚本"]["I2"].value)
    finally:
        workbook.close()


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _complete_generation_stages(
    runtime,
    workbook: Path,
    audit: Path,
    *,
    machine_pass: bool,
) -> None:
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
        status = (
            StageStatus.BLOCKED
            if stage_id == "machine_audit" and not machine_pass
            else StageStatus.SUCCEEDED
        )
        error = (
            StageError(
                StageErrorCategory.CONTRACT,
                "machine_audit_failed",
                "机器审计未通过",
            )
            if status == StageStatus.BLOCKED
            else None
        )
        finish_pipeline_stage(
            runtime,
            stage_id,
            status,
            output_sha256=f"output-{stage_id}",
            output_refs=refs,
            error=error,
        )


def _quality_pass_report() -> dict:
    return {
        "schema_version": "v0.2.1",
        "status": "complete",
        "human_review_status": "quality_pass",
        "projects": [{"pending_shot_count": 0}],
        "summary": {},
        "rule_change_candidates": [],
    }


if __name__ == "__main__":
    unittest.main()
