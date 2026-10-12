import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from mvp_v2.asset_package import AssetPackageError
from mvp_v2.image_understanding import VisionProviderError
from mvp_v2.pipeline_runtime import fork_pipeline_run, open_pipeline_run
from mvp_v2.pipeline_stages import (
    AutoPipelineStageError,
    run_initial_auto_pipeline_stages,
    run_input_freeze_stage,
    run_media_manifest_stage,
    run_source_catalog_stage,
    run_visual_analysis_stage,
)
from mvp_v2.run_contract import sha256_file


class AutoPipelineStageTests(unittest.TestCase):
    def test_downstream_fork_does_not_recompute_frozen_initial_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._write_source_workbook(source_root)
            brief = {"项目名称": "冻结发布会", "项目区域文件名": "冻结项目"}
            manifest_path = root / "manual-manifest.jsonl"
            analysis_path = root / "manual-analysis.jsonl"
            manifest_path.write_text(
                '{"image_id":"img-1","原图路径":"/tmp/anonymous.png"}\n',
                encoding="utf-8",
            )
            analysis_path.write_text(
                '{"image_id":"img-1","caption":"匿名画面","tags":["匿名"],"source":"minimax"}\n',
                encoding="utf-8",
            )
            cache_root = root / "assets"
            with open_pipeline_run(
                brief,
                cache_root,
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as parent:
                original = run_initial_auto_pipeline_stages(
                    parent,
                    brief=brief,
                    config={"source_view": "human"},
                    source_root=source_root,
                    cache_root=cache_root,
                    source_view="human",
                    api_key="",
                    manual_manifest_path=manifest_path,
                    manual_analysis_path=analysis_path,
                )
                parent_id = parent.run_id

            with fork_pipeline_run(
                parent_id,
                cache_root,
                brief=brief,
                source_root=source_root,
                cache_fingerprint="changed-fingerprint",
                from_stage="script_plan",
            ) as child:
                before = json.loads(child.manifest_path.read_text(encoding="utf-8"))
                reused = run_initial_auto_pipeline_stages(
                    child,
                    brief=brief,
                    config={"source_view": "changed"},
                    source_root=root / "missing-source",
                    cache_root=cache_root,
                    source_view="ai-spec",
                    api_key="",
                    manual_manifest_path=root / "missing-manifest.jsonl",
                    manual_analysis_path=root / "missing-analysis.jsonl",
                    asset_package=root / "missing-package",
                )
                after = json.loads(child.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(reused.manifest_rows, original.manifest_rows)
            self.assertEqual(reused.analysis_rows, original.analysis_rows)
            for stage_id in (
                "input_freeze",
                "source_catalog",
                "media_manifest",
                "visual_analysis",
            ):
                self.assertEqual(
                    after["stages"][stage_id]["attempt"],
                    before["stages"][stage_id]["attempt"],
                )
                self.assertEqual(
                    after["stages"][stage_id]["output_sha256"],
                    before["stages"][stage_id]["output_sha256"],
                )

    def test_input_freeze_blocks_manifest_category_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief = {
                "项目名称": "匿名展会",
                "脚本类型": "展会",
                "视频时长": "90s",
            }
            with open_pipeline_run(
                brief,
                root / "assets",
                root / "source",
                cache_fingerprint="visual-fingerprint",
                category_id="launch",
            ) as state:
                with self.assertRaises(AutoPipelineStageError) as captured:
                    run_input_freeze_stage(
                        state,
                        brief=brief,
                        config={},
                        source_root=root / "source",
                    )

            self.assertEqual(captured.exception.error.code, "category_route_mismatch")

    def test_manual_inputs_complete_four_stages_and_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._write_source_workbook(source_root)
            brief = {"项目名称": "匿名发布会", "项目区域文件名": "匿名项目"}
            manifest_path = root / "manual-manifest.jsonl"
            analysis_path = root / "manual-analysis.jsonl"
            manifest_path.write_text(
                '{"image_id":"img-1","原图路径":"/tmp/anonymous.png"}\n',
                encoding="utf-8",
            )
            analysis_path.write_text(
                '{"image_id":"img-1","caption":"匿名画面","tags":["匿名"],"source":"minimax"}\n',
                encoding="utf-8",
            )
            with open_pipeline_run(
                brief,
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                kwargs = {
                    "brief": brief,
                    "config": {"source_view": "human"},
                    "source_root": source_root,
                    "cache_root": root / "assets",
                    "source_view": "human",
                    "api_key": "must-not-be-persisted",
                    "manual_manifest_path": manifest_path,
                    "manual_analysis_path": analysis_path,
                }
                assets = run_initial_auto_pipeline_stages(state, **kwargs)
                reused = run_initial_auto_pipeline_stages(state, **kwargs)

            self.assertEqual(assets.manifest_rows, reused.manifest_rows)
            self.assertEqual(assets.analysis_rows, reused.analysis_rows)
            run_manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
            self.assertNotIn("must-not-be-persisted", state.manifest_path.read_text(encoding="utf-8"))
            for stage_id in (
                "input_freeze",
                "source_catalog",
                "media_manifest",
                "visual_analysis",
            ):
                stage = run_manifest["stages"][stage_id]
                self.assertEqual(stage["status"], "succeeded")
                self.assertEqual(stage["attempt"], 1)
                self.assertTrue(stage["input_sha256"])
                self.assertTrue(stage["output_sha256"])
                self.assertTrue(stage["output_refs"])
            self.assertEqual(
                Path(run_manifest["stages"]["media_manifest"]["output_refs"][0]),
                assets.manifest_path.resolve(),
            )
            self.assertEqual(
                Path(run_manifest["stages"]["visual_analysis"]["output_refs"][0]),
                assets.analysis_path.resolve(),
            )

    def test_input_freeze_and_source_catalog_write_hashes_refs_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            workbook_path = self._write_source_workbook(source_root)
            brief = {
                "项目名称": "匿名发布会",
                "项目区域文件名": "匿名项目",
                "access_token": "brief-secret",
            }
            cache_dir = root / "assets"

            with open_pipeline_run(
                brief,
                cache_dir,
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                freeze_path = run_input_freeze_stage(
                    state,
                    brief=brief,
                    config={
                        "source_view": "human",
                        "api_key": "config-secret",
                        "minimax_command_sha256": "command-hash",
                    },
                    source_root=source_root,
                    brief_ref=root / "brief.txt",
                )
                catalog = run_source_catalog_stage(
                    state,
                    input_freeze_path=freeze_path,
                    brief=brief,
                    source_root=source_root,
                    source_view="human",
                )
                run_input_freeze_stage(
                    state,
                    brief=brief,
                    config={
                        "source_view": "human",
                        "api_key": "config-secret",
                        "minimax_command_sha256": "command-hash",
                    },
                    source_root=source_root,
                    brief_ref=root / "brief.txt",
                )

            freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
            self.assertEqual(freeze["brief"]["access_token"], "[REDACTED]")
            self.assertEqual(freeze["config"]["api_key"], "[REDACTED]")
            self.assertNotIn("config-secret", state.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(freeze["source_workbook"]["sha256"], sha256_file(workbook_path))

            catalog_payload = json.loads(catalog.path.read_text(encoding="utf-8"))
            self.assertEqual(catalog_payload["record_count"], 2)
            self.assertEqual(catalog_payload["template_count"], 1)
            self.assertEqual(catalog_payload["creative_count"], 1)
            self.assertEqual(
                {
                    (row["source_file"], row["source_sheet"], row["source_row"])
                    for row in catalog_payload["source_refs"]
                },
                {
                    ("M+发布会类通用脚本库.xlsx", "模版", 7),
                    ("M+发布会类通用脚本库.xlsx", "创意库", 7),
                },
            )
            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["stages"]["input_freeze"]["status"], "succeeded")
            self.assertEqual(manifest["stages"]["input_freeze"]["attempt"], 1)
            self.assertEqual(manifest["stages"]["source_catalog"]["status"], "succeeded")
            self.assertTrue(manifest["stages"]["input_freeze"]["input_sha256"])
            self.assertTrue(manifest["stages"]["source_catalog"]["output_sha256"])
            self.assertEqual(
                manifest["stages"]["source_catalog"]["output_refs"],
                [str(catalog.path.resolve())],
            )

    def test_missing_source_workbook_blocks_source_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            brief = {"项目名称": "匿名发布会"}
            with open_pipeline_run(
                brief,
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                freeze_path = run_input_freeze_stage(
                    state,
                    brief=brief,
                    config={"source_view": "human"},
                    source_root=source_root,
                )
                with self.assertRaises(AutoPipelineStageError) as context:
                    run_source_catalog_stage(
                        state,
                        input_freeze_path=freeze_path,
                        brief=brief,
                        source_root=source_root,
                        source_view="human",
                    )

            self.assertEqual(context.exception.status, "blocked")
            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
            stage = manifest["stages"]["source_catalog"]
            self.assertEqual(stage["status"], "blocked")
            self.assertEqual(stage["error"]["category"], "input")
            self.assertEqual(stage["error"]["code"], "source_workbook_missing")

    def test_versioned_source_workbook_name_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._write_source_workbook(source_root)
            workbook_path = self._write_source_workbook(
                source_root / "新整理脚本",
                "M+发布会类通用脚本库 AI版v20260720.xlsx",
            )
            self._write_source_workbook(
                source_root / "填写示例版",
                "M+发布会类通用脚本库 填写示例版.xlsx",
            )
            self._write_source_workbook(source_root / "可运行版")
            (source_root / ".~M+发布会类通用脚本库 AI版v99999999.xlsx").write_text(
                "temporary",
                encoding="utf-8",
            )
            (source_root / "~$M+发布会类通用脚本库 AI版v99999999.xlsx").write_text(
                "temporary",
                encoding="utf-8",
            )
            brief = {"项目名称": "匿名发布会"}
            with open_pipeline_run(
                brief,
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                freeze_path = run_input_freeze_stage(
                    state,
                    brief=brief,
                    config={"source_view": "human"},
                    source_root=source_root,
                )
                catalog = run_source_catalog_stage(
                    state,
                    input_freeze_path=freeze_path,
                    brief=brief,
                    source_root=source_root,
                    source_view="human",
                )

            freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
            self.assertEqual(freeze["source_workbook"]["path"], str(workbook_path.resolve()))
            self.assertEqual(catalog.template_count, 1)
            self.assertEqual(catalog.creative_count, 1)

    def test_invalid_source_view_is_contract_blocked_not_type_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._write_source_workbook(source_root)
            brief = {"项目名称": "匿名发布会"}
            with open_pipeline_run(
                brief,
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                freeze_path = run_input_freeze_stage(
                    state,
                    brief=brief,
                    config={"source_view": "invalid"},
                    source_root=source_root,
                )
                with self.assertRaises(AutoPipelineStageError) as context:
                    run_source_catalog_stage(
                        state,
                        input_freeze_path=freeze_path,
                        brief=brief,
                        source_root=source_root,
                        source_view="invalid",
                    )

            self.assertEqual(context.exception.status, "blocked")
            stage = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]["source_catalog"]
            self.assertEqual(stage["error"]["category"], "contract")
            self.assertEqual(stage["error"]["code"], "source_view_invalid")
            self.assertIn("invalid", stage["error"]["message"])

    def test_initial_orchestration_rejects_unpaired_manual_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._write_source_workbook(source_root)
            manifest_path = root / "manifest.jsonl"
            manifest_path.write_text('{"image_id":"img-1"}\n', encoding="utf-8")
            with open_pipeline_run(
                {"项目名称": "匿名发布会"},
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                with self.assertRaisesRegex(ValueError, "同时提供"):
                    run_initial_auto_pipeline_stages(
                        state,
                        brief={"项目名称": "匿名发布会"},
                        config={"source_view": "human"},
                        source_root=source_root,
                        cache_root=root / "assets",
                        source_view="human",
                        api_key="",
                        manual_manifest_path=manifest_path,
                    )

            stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
            self.assertTrue(all(stage["attempt"] == 0 for stage in stages.values()))

    def test_missing_media_produces_empty_warning_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._write_source_workbook(source_root)
            brief = {"项目名称": "匿名发布会", "项目区域文件名": "missing-media"}
            with open_pipeline_run(
                brief,
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                freeze_path = run_input_freeze_stage(
                    state,
                    brief=brief,
                    config={"source_view": "human"},
                    source_root=source_root,
                )
                run_source_catalog_stage(
                    state,
                    input_freeze_path=freeze_path,
                    brief=brief,
                    source_root=source_root,
                    source_view="human",
                )
                result = run_media_manifest_stage(
                    state,
                    input_freeze_path=freeze_path,
                    brief=brief,
                    source_root=source_root,
                    cache_root=root / "assets",
                )

            stage = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]["media_manifest"]
            self.assertEqual(stage["status"], "succeeded_with_warnings")
            self.assertEqual(result.manifest_rows, [])
            self.assertEqual(result.manifest_path.read_text(encoding="utf-8"), "")

    def test_package_media_content_controls_manifest_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._write_source_workbook(source_root)
            brief = {"项目名称": "匿名发布会", "项目区域文件名": "missing-media"}
            first_rows = [{
                "image_id": "media-1",
                "原图路径": "/tmp/reference-1.png",
                "asset_role": "source_reference",
                "analysis_required": False,
            }]
            changed_rows = [*first_rows, {
                "image_id": "media-2",
                "原图路径": "/tmp/reference-2.png",
                "asset_role": "source_reference",
                "analysis_required": False,
            }]
            with open_pipeline_run(
                brief,
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                freeze_path = run_input_freeze_stage(
                    state,
                    brief=brief,
                    config={"source_view": "human"},
                    source_root=source_root,
                )
                with patch(
                    "mvp_v2.pipeline_stages.package_media_for_brief",
                    side_effect=[
                        (first_rows, []),
                        (first_rows, []),
                        (changed_rows, []),
                    ],
                ):
                    first = run_media_manifest_stage(
                        state,
                        input_freeze_path=freeze_path,
                        brief=brief,
                        source_root=source_root,
                        cache_root=root / "assets",
                        asset_package=root / "package.json",
                    )
                    reused = run_media_manifest_stage(
                        state,
                        input_freeze_path=freeze_path,
                        brief=brief,
                        source_root=source_root,
                        cache_root=root / "assets",
                        asset_package=root / "package.json",
                    )
                    reused_stage = json.loads(
                        state.manifest_path.read_text(encoding="utf-8")
                    )["stages"]["media_manifest"]
                    changed = run_media_manifest_stage(
                        state,
                        input_freeze_path=freeze_path,
                        brief=brief,
                        source_root=source_root,
                        cache_root=root / "assets",
                        asset_package=root / "package.json",
                    )

            final_stage = json.loads(
                state.manifest_path.read_text(encoding="utf-8")
            )["stages"]["media_manifest"]
            self.assertEqual(first.manifest_rows, reused.manifest_rows)
            self.assertEqual(reused_stage["attempt"], 1)
            self.assertEqual(final_stage["attempt"], 2)
            self.assertEqual(len(changed.manifest_rows), 2)

    def test_package_prefetch_error_is_recorded_as_blocked_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._write_source_workbook(source_root)
            brief = {"项目名称": "匿名发布会", "项目区域文件名": "missing-media"}
            with open_pipeline_run(
                brief,
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                freeze_path = run_input_freeze_stage(
                    state,
                    brief=brief,
                    config={"source_view": "human"},
                    source_root=source_root,
                )
                with patch(
                    "mvp_v2.pipeline_stages.package_media_for_brief",
                    side_effect=AssetPackageError("资产包类别不一致"),
                ):
                    with self.assertRaises(AutoPipelineStageError) as captured:
                        run_media_manifest_stage(
                            state,
                            input_freeze_path=freeze_path,
                            brief=brief,
                            source_root=source_root,
                            cache_root=root / "assets",
                            asset_package=root / "package.json",
                        )

            stage = json.loads(
                state.manifest_path.read_text(encoding="utf-8")
            )["stages"]["media_manifest"]
            self.assertEqual(captured.exception.status, "blocked")
            self.assertEqual(stage["status"], "blocked")
            self.assertEqual(stage["error"]["category"], "contract")

    def test_visual_technical_error_marks_failed_and_keeps_media_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._write_source_workbook(source_root)
            brief = {"项目名称": "匿名发布会", "项目区域文件名": "匿名项目"}
            manifest_path = root / "manual-manifest.jsonl"
            manifest_path.write_text(
                '{"image_id":"img-1","原图路径":"/tmp/anonymous.png"}\n',
                encoding="utf-8",
            )
            with open_pipeline_run(
                brief,
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                freeze_path = run_input_freeze_stage(
                    state,
                    brief=brief,
                    config={"source_view": "human"},
                    source_root=source_root,
                )
                run_source_catalog_stage(
                    state,
                    input_freeze_path=freeze_path,
                    brief=brief,
                    source_root=source_root,
                    source_view="human",
                )
                media = run_media_manifest_stage(
                    state,
                    input_freeze_path=freeze_path,
                    brief=brief,
                    source_root=source_root,
                    cache_root=root / "assets",
                    manual_manifest_path=manifest_path,
                )
                with patch(
                    "mvp_v2.pipeline_stages.analyze_auto_pipeline_media_manifest",
                    side_effect=RuntimeError("provider unavailable"),
                ):
                    with self.assertRaises(AutoPipelineStageError):
                        run_visual_analysis_stage(
                            state,
                            media_manifest=media,
                            api_key="temporary-key",
                        )

            stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
            self.assertEqual(stages["media_manifest"]["status"], "succeeded")
            self.assertTrue(Path(stages["media_manifest"]["output_refs"][0]).is_file())
            self.assertEqual(stages["visual_analysis"]["status"], "failed")
            self.assertEqual(stages["visual_analysis"]["error"]["category"], "execution")

    def test_source_reference_manifest_does_not_call_vision_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._write_source_workbook(source_root)
            brief = {"项目名称": "匿名发布会", "项目区域文件名": "匿名项目"}
            manifest_path = root / "source-reference.jsonl"
            manifest_path.write_text(json.dumps({
                "image_id": "source-ref-1",
                "原图路径": "/tmp/source-reference.png",
                "asset_role": "source_reference",
                "analysis_required": False,
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            with open_pipeline_run(
                brief,
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                freeze_path = run_input_freeze_stage(
                    state,
                    brief=brief,
                    config={"source_view": "human"},
                    source_root=source_root,
                )
                media = run_media_manifest_stage(
                    state,
                    input_freeze_path=freeze_path,
                    brief=brief,
                    source_root=source_root,
                    cache_root=root / "assets",
                    manual_manifest_path=manifest_path,
                )
                with patch(
                    "mvp_v2.pipeline_stages.analyze_auto_pipeline_media_manifest",
                    side_effect=AssertionError("源表参考图不应重新调用视觉模型"),
                ):
                    assets = run_visual_analysis_stage(
                        state,
                        media_manifest=media,
                        api_key="temporary-key",
                    )

            stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
            self.assertEqual(stages["visual_analysis"]["status"], "succeeded_with_warnings")
            self.assertEqual(assets.analysis_rows, [])
            self.assertEqual(assets.generated_analysis_count, 0)

    def test_visual_provider_failure_keeps_partial_analysis_as_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            self._write_source_workbook(source_root)
            brief = {"项目名称": "匿名发布会", "项目区域文件名": "匿名项目"}
            manifest_path = root / "manual-manifest.jsonl"
            manifest_path.write_text(
                '{"image_id":"img-1","原图路径":"/tmp/anonymous.png"}\n',
                encoding="utf-8",
            )
            with open_pipeline_run(
                brief,
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                freeze_path = run_input_freeze_stage(
                    state,
                    brief=brief,
                    config={"source_view": "human"},
                    source_root=source_root,
                )
                media = run_media_manifest_stage(
                    state,
                    input_freeze_path=freeze_path,
                    brief=brief,
                    source_root=source_root,
                    cache_root=root / "assets",
                    manual_manifest_path=manifest_path,
                )

                def partial_failure(*args, **kwargs):
                    analysis = media.cache_dir / "image_analysis.minimax.jsonl"
                    analysis.parent.mkdir(parents=True, exist_ok=True)
                    analysis.write_text(
                        '{"image_id":"img-1","caption":"部分成功","tags":["现场"],"source":"minimax"}\n',
                        encoding="utf-8",
                    )
                    raise VisionProviderError("provider timeout")

                with patch(
                    "mvp_v2.pipeline_stages.analyze_auto_pipeline_media_manifest",
                    side_effect=partial_failure,
                ):
                    assets = run_visual_analysis_stage(
                        state,
                        media_manifest=media,
                        api_key="temporary-key",
                    )

            stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
            self.assertEqual(stages["visual_analysis"]["status"], "succeeded_with_warnings")
            self.assertEqual(len(assets.analysis_rows), 1)
            self.assertEqual(assets.reused_analysis_count, 0)
            self.assertEqual(assets.generated_analysis_count, 1)

    @staticmethod
    def _write_source_workbook(
        source_root: Path,
        filename: str = "M+发布会类通用脚本库.xlsx",
    ) -> Path:
        source_root.mkdir(parents=True)
        path = source_root / filename
        workbook = Workbook()
        for index, name in enumerate(("模版", "创意库")):
            sheet = workbook.active if index == 0 else workbook.create_sheet()
            sheet.title = name
            headers = ["镜号", "结构", "时长(秒)", "画面描述", "声音"]
            for column, header in enumerate(headers, start=1):
                sheet.cell(6, column, header)
            sheet.append([])
            for column, value in enumerate(
                ("1", "开场", "60", f"{name}画面", "音乐"),
                start=1,
            ):
                sheet.cell(7, column, value)
        workbook.save(path)
        workbook.close()
        return path


if __name__ == "__main__":
    unittest.main()
