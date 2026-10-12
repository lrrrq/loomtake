from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from mvp_v2.auto_pipeline import AutoPipelineAssets, snapshot_visual_assets
from mvp_v2.cli import main as cli_main
from mvp_v2.excel_io import write_template_output_workbook
from mvp_v2.loop_runner import LoopIteration, LoopResult
from mvp_v2.run_contract import sha256_file
from mvp_v2.stage_contract import StageStatus
from mvp_v2.stage_runtime import finish_pipeline_stage, start_pipeline_stage


class VisualSnapshotTests(unittest.TestCase):
    def test_snapshot_copies_visual_evidence_into_run_and_detaches_shared_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            run_dir = cache_dir / "runs" / "run-1"
            cache_dir.mkdir()
            run_dir.mkdir(parents=True)
            manifest_path = cache_dir / "image_manifest.jsonl"
            analysis_path = cache_dir / "image_analysis.minimax.jsonl"
            manifest_path.write_text('{"image_id":"one"}\n', encoding="utf-8")
            analysis_path.write_text(
                '{"image_id":"one","source":"minimax_api"}\n', encoding="utf-8"
            )
            assets = AutoPipelineAssets(
                cache_dir=cache_dir,
                manifest_path=manifest_path,
                analysis_path=analysis_path,
                report_path=cache_dir / "run_report.json",
                manifest_rows=[{"image_id": "one"}],
                analysis_rows=[{"image_id": "one", "source": "minimax_api"}],
                reused_analysis_count=1,
                generated_analysis_count=0,
                cache_fingerprint="fingerprint",
            )
            source_hashes = (sha256_file(manifest_path), sha256_file(analysis_path))

            snapshot = snapshot_visual_assets(assets, run_dir)

            self.assertEqual(snapshot.manifest_path, run_dir / "image_manifest.jsonl")
            self.assertEqual(snapshot.analysis_path, run_dir / "image_analysis.minimax.jsonl")
            self.assertEqual(
                (sha256_file(snapshot.manifest_path), sha256_file(snapshot.analysis_path)),
                source_hashes,
            )
            self.assertFalse(snapshot.manifest_path.is_symlink())
            self.assertFalse(snapshot.analysis_path.is_symlink())

            manifest_path.write_text('{"image_id":"changed"}\n', encoding="utf-8")
            analysis_path.write_text('{"image_id":"changed"}\n', encoding="utf-8")
            self.assertEqual(
                (sha256_file(snapshot.manifest_path), sha256_file(snapshot.analysis_path)),
                source_hashes,
            )

    def test_auto_pipeline_report_and_run_manifest_bind_run_local_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path = root / "brief.txt"
            brief_path.write_text(
                """视觉快照发布会
项目名称：视觉快照发布会
项目区域文件名：视觉快照项目
脚本类型：发布会
品牌或客户：测试品牌
行业或活动类别：科技发布会
视频时长：60s
风格调性：科技感
必须出现的内容：品牌logo
禁忌或不能出现的内容：竞品
素材目录或素材文件：视觉快照项目
""",
                encoding="utf-8",
            )
            manifest_path = root / "manual-manifest.jsonl"
            analysis_path = root / "manual-analysis.jsonl"
            manifest_path.write_text('{"image_id":"one"}\n', encoding="utf-8")
            analysis_path.write_text(
                '{"image_id":"one","source":"minimax_api"}\n', encoding="utf-8"
            )
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            source_root = root / "source"
            source_root.mkdir()
            workbook = Workbook()
            template = workbook.active
            template.title = "模版"
            for column, header in enumerate(
                ("镜号", "结构", "时长(秒)", "画面描述", "声音"),
                start=1,
            ):
                template.cell(6, column, header)
            for column, value in enumerate(
                ("1", "开场", "60", "匿名画面", "音乐"),
                start=1,
            ):
                template.cell(7, column, value)
            workbook.create_sheet("创意库")
            workbook.save(source_root / "M+发布会类通用脚本库.xlsx")
            workbook.close()

            def fake_loop(briefs, source_root, run_output, **kwargs):
                workbook = Path(run_output)
                rows = [
                    {"结构": f"结构{index}", "镜号": str(index), "时长(秒)": 60 if index == 1 else 0}
                    for index in range(1, 19)
                ]
                details = [
                    {"项目名称": "视觉快照发布会", "slot_index": index, "模板镜号": str(index)}
                    for index in range(1, 19)
                ]
                write_template_output_workbook(workbook, rows, details, [])
                audit_path = Path(str(workbook) + ".audit.json")
                audit = {"status": "pass", "hard_fail_count": 0, "warning_count": 0, "score": 100}
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                log_path = Path(str(workbook) + ".loop.json")
                log_path.write_text("{}", encoding="utf-8")
                _complete_generation_stages(kwargs["runtime"], workbook, audit_path)
                return LoopResult(
                    exit_code=0,
                    output_path=workbook,
                    audit_path=audit_path,
                    log_path=log_path,
                    iterations=[LoopIteration(1, workbook, audit_path, audit)],
                    passed_iteration=1,
                )

            def fake_review(path, workbook_path):
                target = Path(path)
                target.write_text("review", encoding="utf-8")
                return target

            with patch("mvp_v2.cli.run_generation_loop", side_effect=fake_loop), patch(
                "mvp_v2.cli._write_review_html_if_requested", side_effect=fake_review
            ):
                code = cli_main([
                    "--brief-file", str(brief_path),
                    "--output", str(output),
                    "--source-root", str(source_root),
                    "--asset-cache-root", str(cache_root),
                    "--auto-pipeline",
                    "--image-manifest-jsonl", str(manifest_path),
                    "--image-analysis-jsonl", str(analysis_path),
                ])

            self.assertEqual(code, 3)
            cache_dir = next(path for path in cache_root.iterdir() if path.is_dir())
            self.assertFalse((cache_dir / "CURRENT").exists())
            run_dir = next((cache_dir / "runs").iterdir())
            run_manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual(set(run_manifest["visual_snapshots"]), {"image_manifest", "image_analysis"})
            for name, path_key, hash_key in (
                ("image_manifest", "manifest_path", "manifest_sha256"),
                ("image_analysis", "analysis_path", "analysis_sha256"),
            ):
                record = run_manifest["visual_snapshots"][name]
                snapshot_path = Path(record["path"])
                self.assertEqual(snapshot_path.parent.resolve(), run_dir.resolve())
                self.assertEqual(Path(report[path_key]).resolve(), snapshot_path.resolve())
                self.assertEqual(report[hash_key], sha256_file(snapshot_path))
                self.assertEqual(record["sha256"], report[hash_key])
                self.assertEqual(record["size_bytes"], snapshot_path.stat().st_size)


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


if __name__ == "__main__":
    unittest.main()
