import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from mvp_v2.auto_pipeline import project_cache_dir
from mvp_v2.auto_pipeline import current_visual_cache_fingerprint
from mvp_v2.cli import main as cli_main
from mvp_v2.excel_io import write_template_output_workbook
from mvp_v2.loop_runner import LoopIteration, LoopResult
from mvp_v2.pipeline_runtime import resolve_current_release, resolve_pipeline_run
from mvp_v2.review_package import export_review_workbook
from mvp_v2.run_state import exclusive_lock
from mvp_v2.stage_contract import StageStatus
from mvp_v2.stage_runtime import finish_pipeline_stage, start_pipeline_stage


BRIEF = """测试发布会
项目名称：测试发布会
项目区域文件名：测试项目
脚本类型：发布会
品牌或客户：测试品牌
行业或活动类别：科技发布会
视频时长：60s
风格调性：科技感
必须出现的内容：品牌logo
禁忌或不能出现的内容：竞品
素材目录或素材文件：测试项目
"""


class PipelineCliTests(unittest.TestCase):
    def test_invalid_launch_brief_exits_one_without_pipeline_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path = root / "invalid-brief.txt"
            brief_path.write_text(BRIEF.replace("视频时长：60s", "视频时长：59s"), encoding="utf-8")
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"

            with patch("mvp_v2.cli.run_generation_loop") as generation:
                code = cli_main([
                    "--brief-file", str(brief_path),
                    "--output", str(output),
                    "--source-root", str(root / "source"),
                    "--asset-cache-root", str(cache_root),
                    "--auto-pipeline",
                ])

            self.assertEqual(code, 1)
            generation.assert_not_called()
            self.assertFalse(output.exists())
            self.assertFalse(cache_root.exists())

    def test_auto_pipeline_pending_waits_in_run_directory_without_publishing_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            generation_outputs = []

            def fake_loop(briefs, source_root, run_output, **kwargs):
                run_output = Path(run_output)
                generation_outputs.append(run_output)
                self.assertIn("runs", run_output.parts)
                self._write_pending_workbook(run_output)
                audit_path = Path(str(run_output) + ".audit.json")
                audit = {
                    "status": "pass",
                    "hard_fail_count": 0,
                    "warning_count": 0,
                    "score": 100,
                    "blocking_items": [],
                }
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                log_path = Path(str(run_output) + ".loop.json")
                log_path.write_text('{"iteration":1}\n', encoding="utf-8")
                self._complete_generation_stages(kwargs["runtime"], run_output, audit_path)
                return LoopResult(
                    exit_code=0,
                    output_path=run_output,
                    audit_path=audit_path,
                    log_path=log_path,
                    iterations=[LoopIteration(1, run_output, audit_path, audit)],
                    passed_iteration=1,
                )

            def fake_review(path, workbook_path):
                target = Path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"review:{workbook_path}", encoding="utf-8")
                return target

            with patch("mvp_v2.cli.run_generation_loop", side_effect=fake_loop), patch(
                "mvp_v2.cli._write_review_html_if_requested", side_effect=fake_review
            ):
                code = cli_main(self._args(
                    brief_path, output, cache_root, manifest_path, analysis_path
                ) + ["--minimax-command", "/opt/mmx-v2"])

            self.assertEqual(code, 3)
            self.assertEqual(len(generation_outputs), 1)

            cache_dir = project_cache_dir(
                {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                cache_root,
            )
            current_link = cache_dir / "CURRENT"
            self.assertFalse(current_link.exists())
            run_dirs = list((cache_dir / "runs").iterdir())
            self.assertEqual(len(run_dirs), 1)
            current_dir = run_dirs[0]
            current_run_id = current_dir.name
            report = json.loads((current_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertRegex(report["run_id"], r"^[A-Za-z0-9_-]+$")
            self.assertEqual(report["run_id"], current_run_id)
            self.assertTrue(Path(report["run_dir"]).is_dir())
            self.assertTrue(Path(report["run_manifest_path"]).exists())
            self.assertTrue(Path(report["run_ledger_path"]).exists())
            self.assertEqual(Path(report["output_path"]).parent.resolve(), current_dir.resolve())
            self.assertEqual(report["output_sha256"], self._sha256(Path(report["output_path"])))
            run_manifest = json.loads(Path(report["run_manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(run_manifest["status"], "waiting_human_review")
            self.assertEqual(
                run_manifest["cache_fingerprint"],
                current_visual_cache_fingerprint("/opt/mmx-v2"),
            )
            self.assertEqual(set(run_manifest["artifacts"]), {
                "workbook", "audit", "loop_log", "visual_review", "run_report",
            })
            self.assertEqual(
                {Path(record["path"]).parent for record in run_manifest["artifacts"].values()},
                {current_dir.resolve()},
            )
            self.assertTrue(all(Path(record["path"]).is_file() for record in run_manifest["artifacts"].values()))
            self.assertEqual(
                run_manifest["stages"]["human_review"]["status"],
                "waiting_human_review",
            )
            self.assertEqual(
                run_manifest["stages"]["publish_feedback"]["status"],
                "not_started",
            )
            public_paths = {
                "workbook": output,
                "audit": Path(str(output) + ".audit.json"),
                "loop_log": Path(str(output) + ".loop.json"),
                "visual_review": output.with_suffix(".visual_review.html"),
                "run_report": cache_dir / "run_report.json",
            }
            self.assertTrue(all(not path.exists() for path in public_paths.values()))

    def test_auto_pipeline_failure_before_current_switch_keeps_previous_release_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            cache_dir = project_cache_dir(
                {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                cache_root,
            )
            old_run_dir = cache_dir / "runs" / "old-run"
            old_run_dir.mkdir(parents=True)
            current = cache_dir / "CURRENT"
            current.symlink_to(Path("runs") / "old-run")
            old_release = {
                name: old_run_dir / filename
                for name, filename in {
                    "workbook": "result.xlsx",
                    "audit": "result.xlsx.audit.json",
                    "loop_log": "result.xlsx.loop.json",
                    "visual_review": "result.visual_review.html",
                    "run_report": "run_report.json",
                }.items()
            }
            for member in old_release.values():
                member.write_bytes(b"old-release")
            (old_run_dir / "run_manifest.json").write_text(json.dumps({
                "run_id": "old-run",
                "artifacts": {
                    name: {
                        "path": str(path),
                        "sha256": self._sha256(path),
                        "size_bytes": path.stat().st_size,
                    }
                    for name, path in old_release.items()
                },
            }), encoding="utf-8")
            formal_members = {
                "workbook": output,
                "audit": Path(str(output) + ".audit.json"),
                "loop_log": Path(str(output) + ".loop.json"),
                "visual_review": output.with_suffix(".visual_review.html"),
                "run_report": cache_dir / "run_report.json",
            }
            for name, member in formal_members.items():
                member.parent.mkdir(parents=True, exist_ok=True)
                member.symlink_to(old_release[name])

            def fake_loop(briefs, source_root, run_output, **kwargs):
                run_output = Path(run_output)
                self._write_pending_workbook(run_output)
                audit_path = Path(str(run_output) + ".audit.json")
                audit = {
                    "status": "pass",
                    "hard_fail_count": 0,
                    "warning_count": 0,
                    "score": 100,
                    "blocking_items": [],
                }
                audit_path.write_text(json.dumps(audit), encoding="utf-8")
                log_path = Path(str(run_output) + ".loop.json")
                log_path.write_text('{"run":"new"}\n', encoding="utf-8")
                self._complete_generation_stages(kwargs["runtime"], run_output, audit_path)
                return LoopResult(
                    exit_code=0,
                    output_path=run_output,
                    audit_path=audit_path,
                    log_path=log_path,
                    iterations=[LoopIteration(1, run_output, audit_path, audit)],
                    passed_iteration=1,
                )

            def fake_review(path, workbook_path):
                target = Path(path)
                target.write_text("new-review", encoding="utf-8")
                return target

            with patch("mvp_v2.cli.run_generation_loop", side_effect=fake_loop), patch(
                "mvp_v2.cli._write_review_html_if_requested", side_effect=fake_review
            ), patch("mvp_v2.cli.write_run_report", side_effect=RuntimeError("injected before CURRENT")):
                code = cli_main(self._args(
                    brief_path, output, cache_root, manifest_path, analysis_path
                ))

            self.assertEqual(code, 1)
            self.assertEqual(current.resolve(), old_run_dir.resolve())
            self.assertEqual(resolve_current_release(cache_dir), {
                name: path.resolve() for name, path in old_release.items()
            })
            self.assertEqual(
                [path.read_bytes() for path in resolve_current_release(cache_dir).values()],
                [b"old-release"] * 5,
            )
            self.assertEqual([member.read_bytes() for member in formal_members.values()], [b"old-release"] * 5)

    def test_auto_pipeline_migrates_complete_legacy_files_without_setting_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            cache_dir = project_cache_dir(
                {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                cache_root,
            )
            formal_members = self._public_paths(output, cache_dir)
            legacy_content = {
                name: f"legacy:{name}".encode("utf-8")
                for name in formal_members
            }
            for name, member in formal_members.items():
                member.parent.mkdir(parents=True, exist_ok=True)
                member.write_bytes(legacy_content[name])

            with patch("mvp_v2.cli.run_generation_loop", side_effect=self._fake_loop), patch(
                "mvp_v2.cli._write_review_html_if_requested", side_effect=self._fake_review
            ), patch(
                "mvp_v2.review_publish_stages.analyze_review_workbooks",
                return_value=self._quality_pass_report(),
            ), patch(
                "mvp_v2.review_publish_stages._invalid_review_dates",
                return_value=[],
            ), patch("mvp_v2.cli.publish_pipeline_run", side_effect=RuntimeError("new publish failed")):
                code = cli_main(self._args(
                    brief_path, output, cache_root, manifest_path, analysis_path
                ))

            self.assertEqual(code, 1)
            self.assertFalse((cache_dir / "CURRENT").exists())
            legacy_run = next(
                path for path in (cache_dir / "runs").iterdir()
                if path.name.startswith("legacy-")
            )
            legacy_release = resolve_pipeline_run(
                cache_dir,
                legacy_run.name,
                allowed_statuses={"legacy"},
            )
            self.assertEqual(
                {name: path.read_bytes() for name, path in legacy_release.items()},
                legacy_content,
            )
            migration = json.loads((legacy_run / "legacy_migration.json").read_text(encoding="utf-8"))
            self.assertEqual(migration["migration_record"], str((legacy_run / "legacy_migration.json").resolve()))
            self.assertTrue(all(path.is_symlink() for path in formal_members.values()))
            self.assertEqual(
                {name: path.read_bytes() for name, path in formal_members.items()},
                legacy_content,
            )

    def test_legacy_migration_normal_cli_publish_path_has_no_current_or_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            cache_dir = project_cache_dir(
                {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                cache_root,
            )
            formal_members = self._public_paths(output, cache_dir)
            for name, path in formal_members.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"anonymous legacy {name}".encode("utf-8"))
            args = self._args(brief_path, output, cache_root, manifest_path, analysis_path)

            with patch("mvp_v2.cli.run_generation_loop", side_effect=self._fake_loop), patch(
                "mvp_v2.cli._write_review_html_if_requested", side_effect=self._fake_review
            ):
                self.assertEqual(cli_main(args), 3)

            active_run = next(
                path for path in (cache_dir / "runs").iterdir()
                if not path.name.startswith("legacy-")
            )
            review = export_review_workbook(
                cache_dir,
                root / "reviews" / "review.xlsx",
                run_id=active_run.name,
            )
            self._approve_review_workbook(review)
            with patch("mvp_v2.cli.run_generation_loop", side_effect=AssertionError("不得重跑生成")):
                self.assertEqual(cli_main(args + [
                    "--resume", active_run.name,
                    "--review-workbook", str(review),
                ]), 0)

            legacy_run = next(
                path for path in (cache_dir / "runs").iterdir()
                if path.name.startswith("legacy-")
            )
            migrated = resolve_pipeline_run(
                cache_dir,
                legacy_run.name,
                allowed_statuses={"legacy"},
            )
            self.assertEqual(
                {name: path.read_bytes() for name, path in migrated.items()},
                {name: f"anonymous legacy {name}".encode("utf-8") for name in formal_members},
            )
            self.assertTrue(all(path.is_symlink() for path in formal_members.values()))
            self.assertEqual(
                {name: path.resolve() for name, path in formal_members.items()},
                migrated,
            )
            self.assertEqual(
                resolve_pipeline_run(cache_dir, active_run.name, allowed_statuses={"succeeded"})["workbook"].resolve(),
                (active_run / "result.xlsx").resolve(),
            )
            self.assertFalse((cache_dir / "CURRENT").exists())
            self.assertFalse((cache_dir / "workspace.sqlite3").exists())
            self.assertFalse((cache_dir / "outbox").exists())

    def test_auto_pipeline_rejects_partial_legacy_files_without_changing_public_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            cache_dir = project_cache_dir(
                {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                cache_root,
            )
            formal_members = self._public_paths(output, cache_dir)
            for name in ("workbook", "audit"):
                formal_members[name].parent.mkdir(parents=True, exist_ok=True)
                formal_members[name].write_bytes(f"partial:{name}".encode("utf-8"))

            with patch("mvp_v2.cli.run_generation_loop", side_effect=self._fake_loop), patch(
                "mvp_v2.cli._write_review_html_if_requested", side_effect=self._fake_review
            ), patch(
                "mvp_v2.review_publish_stages.analyze_review_workbooks",
                return_value=self._quality_pass_report(),
            ), patch(
                "mvp_v2.review_publish_stages._invalid_review_dates",
                return_value=[],
            ):
                code = cli_main(self._args(
                    brief_path, output, cache_root, manifest_path, analysis_path
                ))

            self.assertEqual(code, 1)
            self.assertFalse((cache_dir / "CURRENT").exists())
            self.assertFalse(formal_members["workbook"].is_symlink())
            self.assertFalse(formal_members["audit"].is_symlink())
            self.assertEqual(formal_members["workbook"].read_bytes(), b"partial:workbook")
            self.assertEqual(formal_members["audit"].read_bytes(), b"partial:audit")
            self.assertFalse(formal_members["loop_log"].exists())
            self.assertFalse(formal_members["visual_review"].exists())
            self.assertFalse(formal_members["run_report"].exists())

    def test_auto_pipeline_relative_cache_root_creates_resolvable_public_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            previous_cwd = Path.cwd()
            os.chdir(root)
            try:
                output = Path("formal/result.xlsx")
                cache_root = Path("assets")
                with patch("mvp_v2.cli.run_generation_loop", side_effect=self._fake_loop), patch(
                    "mvp_v2.cli._write_review_html_if_requested", side_effect=self._fake_review
                ), patch(
                    "mvp_v2.review_publish_stages.analyze_review_workbooks",
                    return_value=self._quality_pass_report(),
                ), patch(
                    "mvp_v2.review_publish_stages._invalid_review_dates",
                    return_value=[],
                ):
                    code = cli_main(self._args(
                        brief_path, output, cache_root, manifest_path, analysis_path
                    ))

                cache_dir = project_cache_dir(
                    {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                    cache_root,
                )
                formal_members = self._public_paths(output, cache_dir)
                self.assertEqual(code, 0)
                run = resolve_pipeline_run(
                    cache_dir,
                    next((cache_dir / "runs").iterdir()).name,
                    allowed_statuses={"succeeded"},
                )
                self.assertTrue(
                    set(formal_members).issubset(run),
                    "公共链接只覆盖发布工件；权威读取来自显式 run",
                )
                self.assertFalse((cache_dir / "CURRENT").exists())
            finally:
                os.chdir(previous_cwd)

    def test_auto_pipeline_lock_conflict_exits_one_without_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            cache_dir = project_cache_dir(
                {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                cache_root,
            )

            with exclusive_lock(cache_dir / ".pipeline.lock"), patch(
                "mvp_v2.cli.run_generation_loop"
            ) as generation:
                code = cli_main(self._args(
                    brief_path, output, cache_root, manifest_path, analysis_path
                ))

            self.assertEqual(code, 1)
            generation.assert_not_called()
            self.assertFalse(output.exists())

    def test_pending_run_resumes_after_human_review_and_publishes_same_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            args = self._args(
                brief_path, output, cache_root, manifest_path, analysis_path
            )
            with patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=self._fake_loop,
            ), patch(
                "mvp_v2.cli._write_review_html_if_requested",
                side_effect=self._fake_review,
            ):
                first_code = cli_main(args)

            cache_dir = project_cache_dir(
                {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                cache_root,
            )
            run_dir = next((cache_dir / "runs").iterdir())
            report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual(first_code, 3)
            self.assertFalse((cache_dir / "CURRENT").exists())
            source_workbook = Path(report["output_path"])
            source_hash = hashlib.sha256(source_workbook.read_bytes()).hexdigest()
            review_workbook = export_review_workbook(
                cache_dir,
                root / "reviews" / "review.xlsx",
                run_id=run_dir.name,
            )
            self._approve_review_workbook(review_workbook)

            with patch(
                "mvp_v2.cli.run_initial_auto_pipeline_stages",
                side_effect=AssertionError("导入复核结论不得重跑输入或素材节点"),
            ), patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=AssertionError("恢复人工节点不得重跑生成"),
            ):
                resumed_code = cli_main(args + [
                    "--resume", run_dir.name,
                    "--review-workbook", str(review_workbook),
                ])

            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(resumed_code, 0)
            self.assertFalse((cache_dir / "CURRENT").exists())
            self.assertEqual(
                set(resolve_pipeline_run(cache_dir, run_dir.name, allowed_statuses={"succeeded"})),
                set(manifest["artifacts"]),
            )
            self.assertTrue(manifest["bundle_finalized"])
            self.assertEqual(manifest["stages"]["human_review"]["status"], "succeeded")
            self.assertEqual(manifest["stages"]["human_review"]["attempt"], 2)
            self.assertEqual(manifest["stages"]["publish_feedback"]["status"], "succeeded")
            self.assertEqual(manifest["stages"]["machine_audit"]["attempt"], 1)
            self.assertEqual(len(list((cache_dir / "runs").iterdir())), 1)
            self.assertEqual(
                hashlib.sha256(source_workbook.read_bytes()).hexdigest(),
                source_hash,
            )

    def test_failed_publish_can_retry_only_publish_stage_without_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            args = self._args(
                brief_path, output, cache_root, manifest_path, analysis_path
            )
            with patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=self._fake_loop,
            ), patch(
                "mvp_v2.cli._write_review_html_if_requested",
                side_effect=self._fake_review,
            ):
                self.assertEqual(cli_main(args), 3)

            cache_dir = project_cache_dir(
                {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                cache_root,
            )
            run_dir = next((cache_dir / "runs").iterdir())
            review_workbook = export_review_workbook(
                cache_dir,
                root / "reviews" / "review.xlsx",
                run_id=run_dir.name,
            )
            self._approve_review_workbook(review_workbook)
            review_args = args + [
                "--resume", run_dir.name,
                "--review-workbook", str(review_workbook),
            ]
            with patch(
                "mvp_v2.cli._prepare_public_release_links",
                side_effect=RuntimeError("link failure"),
            ):
                self.assertEqual(cli_main(review_args), 1)

            failed = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["stages"]["publish_feedback"]["status"], "failed")

            with patch(
                "mvp_v2.cli.run_initial_auto_pipeline_stages",
                side_effect=AssertionError("重试发布节点不得重跑输入或素材节点"),
            ), patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=AssertionError("重试发布节点不得重跑生成"),
            ):
                self.assertEqual(
                    cli_main(review_args + ["--retry-stage", "publish_feedback"]),
                    0,
                )

            completed = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            completed_report = json.loads((run_dir / "run_report.json").read_text(encoding="utf-8"))
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["stages"]["publish_feedback"]["status"], "succeeded")
            self.assertEqual(completed["stages"]["machine_audit"]["attempt"], 1)
            self.assertNotIn("error", completed_report)

    def test_client_review_publish_bundle_contains_review_evidence_and_clean_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            output = root / "formal" / "internal.xlsx"
            client_output = root / "formal" / "客户前终审.xlsx"
            cache_root = root / "assets"
            args = self._args(
                brief_path, output, cache_root, manifest_path, analysis_path
            ) + [
                "--quality-profile", "client-review",
                "--client-output", str(client_output),
            ]
            with patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=self._fake_loop,
            ), patch(
                "mvp_v2.cli._write_review_html_if_requested",
                side_effect=self._fake_review,
            ):
                self.assertEqual(cli_main(args), 3)

            cache_dir = project_cache_dir(
                {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                cache_root,
            )
            run_dir = next((cache_dir / "runs").iterdir())
            review = export_review_workbook(
                cache_dir,
                root / "reviews" / "review.xlsx",
                run_id=run_dir.name,
            )
            self._approve_review_workbook(review)

            with patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=AssertionError("导入人工复核时不得重跑生成"),
            ):
                self.assertEqual(cli_main(args + [
                    "--resume", run_dir.name,
                    "--review-workbook", str(review),
                ]), 0)

            release = resolve_pipeline_run(cache_dir, run_dir.name, allowed_statuses={"succeeded"})
            self.assertEqual(
                set(release),
                {
                    "workbook", "audit", "loop_log", "visual_review", "run_report",
                    "review_workbook", "human_review_report", "client_workbook",
                },
            )
            self.assertTrue(client_output.is_symlink())
            self.assertEqual(client_output.resolve(), release["client_workbook"])
            self.assertFalse((cache_dir / "CURRENT").exists())
            client = load_workbook(release["client_workbook"], read_only=True, data_only=False)
            self.assertEqual(client.sheetnames, ["脚本", "待确认事项"])
            client.close()

    def test_resume_from_and_retry_reopen_only_human_review_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            args = self._args(
                brief_path, output, cache_root, manifest_path, analysis_path
            )
            with patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=self._fake_loop,
            ), patch(
                "mvp_v2.cli._write_review_html_if_requested",
                side_effect=self._fake_review,
            ):
                self.assertEqual(cli_main(args), 3)

            cache_dir = project_cache_dir(
                {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                cache_root,
            )
            run_dir = next((cache_dir / "runs").iterdir())
            with patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=AssertionError("恢复人工节点不得重跑生成"),
            ), patch(
                "mvp_v2.cli._write_review_html_if_requested",
                side_effect=self._fake_review,
            ):
                self.assertEqual(
                    cli_main(args + [
                        "--resume", run_dir.name,
                        "--from-stage", "human_review",
                    ]),
                    3,
                )
                self.assertEqual(
                    cli_main(args + [
                        "--resume", run_dir.name,
                        "--retry-stage", "human_review",
                    ]),
                    3,
                )

            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["stages"]["human_review"]["attempt"], 3)
            self.assertEqual(manifest["stages"]["publish_feedback"]["status"], "invalidated")
            self.assertEqual(manifest["stages"]["machine_audit"]["attempt"], 1)

    def test_fork_from_parent_human_review_reuses_generation_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief_path, manifest_path, analysis_path = self._inputs(root)
            output = root / "formal" / "result.xlsx"
            cache_root = root / "assets"
            args = self._args(
                brief_path, output, cache_root, manifest_path, analysis_path
            )
            with patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=self._fake_loop,
            ), patch(
                "mvp_v2.cli._write_review_html_if_requested",
                side_effect=self._fake_review,
            ):
                self.assertEqual(cli_main(args), 3)

            cache_dir = project_cache_dir(
                {"项目名称": "测试发布会", "项目区域文件名": "测试项目"},
                cache_root,
            )
            parent_dir = next((cache_dir / "runs").iterdir())
            with patch(
                "mvp_v2.cli.run_generation_loop",
                side_effect=AssertionError("从父版本人审节点建子 run 不得重跑生成"),
            ), patch(
                "mvp_v2.cli._write_review_html_if_requested",
                side_effect=self._fake_review,
            ):
                self.assertEqual(
                    cli_main(args + [
                        "--parent-run-id", parent_dir.name,
                        "--from-stage", "human_review",
                    ]),
                    3,
                )

            child_dirs = [
                path for path in (cache_dir / "runs").iterdir()
                if path.name != parent_dir.name
            ]
            self.assertEqual(len(child_dirs), 1)
            child_manifest = json.loads(
                (child_dirs[0] / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(child_manifest["parent_run_id"], parent_dir.name)
            self.assertEqual(
                child_manifest["stages"]["machine_audit"]["status"],
                "succeeded",
            )
            self.assertEqual(
                child_manifest["stages"]["human_review"]["status"],
                "waiting_human_review",
            )

    @staticmethod
    def _args(brief_path, output, cache_root, manifest_path, analysis_path):
        return [
            "--brief-file", str(brief_path),
            "--output", str(output),
            "--source-root", str(Path(output).parent / "source"),
            "--asset-cache-root", str(cache_root),
            "--auto-pipeline",
            "--image-manifest-jsonl", str(manifest_path),
            "--image-analysis-jsonl", str(analysis_path),
        ]

    @staticmethod
    def _inputs(root: Path):
        brief_path = root / "brief.txt"
        brief_path.write_text(BRIEF, encoding="utf-8")
        source_root = root / "formal" / "source"
        source_root.mkdir(parents=True)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "模版"
        for column, header in enumerate(
            ("镜号", "结构", "时长(秒)", "画面描述", "声音"),
            start=1,
        ):
            sheet.cell(6, column, header)
        for column, value in enumerate(
            ("1", "开场", "60", "匿名画面", "音乐"),
            start=1,
        ):
            sheet.cell(7, column, value)
        workbook.create_sheet("创意库")
        workbook.save(source_root / "M+发布会类通用脚本库.xlsx")
        workbook.close()
        manifest_path = root / "manifest.jsonl"
        analysis_path = root / "analysis.jsonl"
        manifest_path.write_text('{"image_id":"img_1","原图路径":"/tmp/one.png"}\n', encoding="utf-8")
        analysis_path.write_text(
            '{"image_id":"img_1","caption":"测试品牌logo","tags":["logo"],"source":"minimax"}\n',
            encoding="utf-8",
        )
        return brief_path, manifest_path, analysis_path

    @staticmethod
    def _write_pending_workbook(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        script_rows = [
            {
                "结构": f"结构{slot}",
                "镜号": str(slot),
                "时长(秒)": 60 if slot == 1 else 0,
                "画面描述": f"镜头{slot}",
                "声音": "音乐",
            }
            for slot in range(1, 19)
        ]
        detail_rows = [
            {"项目名称": "测试发布会", "slot_index": slot, "模板镜号": str(slot)}
            for slot in range(1, 19)
        ]
        write_template_output_workbook(path, script_rows, detail_rows)

    def _fake_loop(self, briefs, source_root, run_output, **kwargs):
        run_output = Path(run_output)
        self._write_pending_workbook(run_output)
        audit_path = Path(str(run_output) + ".audit.json")
        audit = {
            "status": "pass",
            "hard_fail_count": 0,
            "warning_count": 0,
            "score": 100,
            "blocking_items": [],
        }
        audit_path.write_text(json.dumps(audit), encoding="utf-8")
        log_path = Path(str(run_output) + ".loop.json")
        log_path.write_text('{"iteration":1}\n', encoding="utf-8")
        self._complete_generation_stages(kwargs["runtime"], run_output, audit_path)
        return LoopResult(
            exit_code=0,
            output_path=run_output,
            audit_path=audit_path,
            log_path=log_path,
            iterations=[LoopIteration(1, run_output, audit_path, audit)],
            passed_iteration=1,
        )

    @staticmethod
    def _fake_review(path, workbook_path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"review:{workbook_path}", encoding="utf-8")
        return target

    @staticmethod
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

    @staticmethod
    def _quality_pass_report() -> dict:
        return {
            "schema_version": "v0.2.1",
            "status": "complete",
            "human_review_status": "quality_pass",
            "projects": [{"pending_shot_count": 0}],
            "summary": {},
            "rule_change_candidates": [],
        }

    @staticmethod
    def _approve_review_workbook(path: Path) -> None:
        workbook = load_workbook(path)
        review_columns = {
            "剪辑复核": {
                "结构审核", "叙事审核", "节奏审核", "创意审核", "配图审核", "总体审核",
            },
            "相邻镜头复核": {
                "叙事承接", "动作衔接", "运镜衔接", "色彩衔接", "声音衔接", "总体审核",
            },
            "整片复核": {
                "叙事弧", "节奏曲线", "音乐曲线", "高潮与收束", "总体审核",
            },
        }
        for sheet_name, status_columns in review_columns.items():
            sheet = workbook[sheet_name]
            headers = [str(cell.value or "") for cell in sheet[1]]
            positions = {header: index + 1 for index, header in enumerate(headers)}
            for row in range(2, sheet.max_row + 1):
                for column in status_columns:
                    sheet.cell(row, positions[column], "通过")
                if "重大修改" in positions:
                    sheet.cell(row, positions["重大修改"], "否")
                sheet.cell(row, positions["审核人"], "审核员")
                sheet.cell(row, positions["审核日期"], "2026-07-30")
        workbook.save(path)
        workbook.close()

    @staticmethod
    def _public_paths(output: Path, cache_dir: Path) -> dict[str, Path]:
        return {
            "workbook": output,
            "audit": Path(str(output) + ".audit.json"),
            "loop_log": Path(str(output) + ".loop.json"),
            "visual_review": output.with_suffix(".visual_review.html"),
            "run_report": cache_dir / "run_report.json",
        }

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
