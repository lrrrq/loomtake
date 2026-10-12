import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp_v2.pipeline_runtime import (
    finalize_pipeline_run,
    finish_pipeline_stage,
    open_pipeline_run,
    publish_pipeline_run,
    resume_pipeline_run,
    start_pipeline_stage,
)
from mvp_v2.stage_contract import (
    STAGE_ORDER,
    StageError,
    StageErrorCategory,
    StageStatus,
)
from mvp_v2.run_contract import sha256_file
from mvp_v2.runtime_config import configured_source_commit
from mvp_v2.stage_runtime import reused_fork_stage_output


def _read_manifest(state):
    return json.loads(state.manifest_path.read_text(encoding="utf-8"))


def _succeed_before(state, target_stage_id):
    for stage_id in STAGE_ORDER:
        if stage_id.value == target_stage_id:
            return
        current = _read_manifest(state)["stages"][stage_id.value]
        if current["status"] in {"succeeded", "succeeded_with_warnings"}:
            continue
        start_pipeline_stage(state, stage_id, input_sha256="a" * 64)
        finish_pipeline_stage(
            state,
            stage_id,
            StageStatus.SUCCEEDED,
            output_sha256="b" * 64,
        )
    raise AssertionError(f"unknown stage: {target_stage_id}")


class PipelineStageRuntimeTests(unittest.TestCase):
    def test_forked_stage_output_is_frozen_until_stage_is_restarted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "冻结节点项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="stage-runtime",
            ) as state:
                output = state.run_dir / "input_freeze.json"
                output.write_text("{}", encoding="utf-8")
                start_pipeline_stage(state, "input_freeze", input_sha256="a" * 64)
                finish_pipeline_stage(
                    state,
                    "input_freeze",
                    StageStatus.SUCCEEDED,
                    output_sha256=sha256_file(output),
                    output_refs=(str(output.resolve()),),
                )
                manifest = _read_manifest(state)
                manifest["stages"]["input_freeze"]["reused_from_run"] = "parent"
                state.manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False),
                    encoding="utf-8",
                )

                self.assertEqual(
                    reused_fork_stage_output(
                        state,
                        "input_freeze",
                        "input_freeze.json",
                    ).resolve(),
                    output.resolve(),
                )
                start_pipeline_stage(
                    state,
                    "input_freeze",
                    input_sha256="c" * 64,
                )
                self.assertNotIn(
                    "reused_from_run",
                    _read_manifest(state)["stages"]["input_freeze"],
                )

    def test_new_run_initializes_v3_stages_and_records_attempt_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "节点状态项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="stage-runtime",
            ) as state:
                manifest = _read_manifest(state)
                self.assertEqual(manifest["schema_version"], "stage1-pipeline-runtime/v3")
                self.assertEqual(manifest["category_id"], "launch")
                self.assertEqual(
                    set(manifest["stages"]),
                    {stage_id.value for stage_id in STAGE_ORDER},
                )
                for stage_id, stage in manifest["stages"].items():
                    self.assertEqual(stage["stage_id"], stage_id)
                    self.assertEqual(stage["status"], "not_started")
                    self.assertEqual(stage["attempt"], 0)

                start_pipeline_stage(
                    state,
                    "input_freeze",
                    input_sha256="a" * 64,
                    input_refs=("brief://redacted",),
                    log_ref="logs://input-freeze/1",
                )
                finish_pipeline_stage(
                    state,
                    "input_freeze",
                    StageStatus.SUCCEEDED,
                    output_sha256="b" * 64,
                    output_refs=("artifact://frozen-input",),
                )
                stage = _read_manifest(state)["stages"]["input_freeze"]

                self.assertEqual(stage["attempt"], 1)
                self.assertEqual(stage["input_sha256"], "a" * 64)
                self.assertEqual(stage["output_sha256"], "b" * 64)
                self.assertEqual(stage["input_refs"], ["brief://redacted"])
                self.assertEqual(stage["output_refs"], ["artifact://frozen-input"])
                self.assertEqual(stage["log_ref"], "logs://input-freeze/1")
                self.assertTrue(stage["started_at"])
                self.assertTrue(stage["finished_at"])

    def test_resume_and_unchanged_successful_stage_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "幂等恢复项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="resume-idempotent",
            ) as state:
                start_pipeline_stage(state, "input_freeze", input_sha256="a" * 64)
                finish_pipeline_stage(
                    state,
                    "input_freeze",
                    StageStatus.SUCCEEDED,
                    output_sha256="b" * 64,
                )

            before = state.manifest_path.read_bytes()
            for _ in range(2):
                with resume_pipeline_run(state.run_id, cache_dir) as resumed:
                    stage = start_pipeline_stage(
                        resumed,
                        "input_freeze",
                        input_sha256="a" * 64,
                    )
                    self.assertEqual(stage["status"], "succeeded")
                    self.assertEqual(stage["attempt"], 1)
                self.assertEqual(state.manifest_path.read_bytes(), before)

    def test_retry_stage_increments_only_selected_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "单节点重试项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="retry-one-stage",
            ) as state:
                start_pipeline_stage(state, "input_freeze", input_sha256="a" * 64)
                finish_pipeline_stage(
                    state,
                    "input_freeze",
                    StageStatus.BLOCKED,
                    error=StageError(
                        category=StageErrorCategory.INPUT,
                        code="brief_not_ready",
                        message="brief input is not ready",
                        retryable=True,
                    ),
                )

            with resume_pipeline_run(
                state.run_id,
                cache_dir,
                retry_stage="input_freeze",
            ) as resumed:
                manifest = _read_manifest(resumed)
                stages = manifest["stages"]
                self.assertEqual(stages["input_freeze"]["status"], "running")
                self.assertEqual(stages["input_freeze"]["attempt"], 2)
                self.assertIsNone(stages["input_freeze"]["error"])
                self.assertEqual(stages["source_catalog"]["attempt"], 0)
                self.assertEqual(stages["visual_analysis"]["attempt"], 0)
                self.assertEqual(manifest["status"], "running")
                self.assertNotIn("finished_at", manifest)

    def test_stage_cannot_start_before_dependencies_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "依赖门项目"},
                cache_dir=root / "cache",
                source_root=source_root,
                cache_fingerprint="dependency-gate",
            ) as state:
                before = state.manifest_path.read_bytes()
                with self.assertRaisesRegex(ValueError, "source_catalog"):
                    start_pipeline_stage(
                        state,
                        "script_plan",
                        input_sha256="a" * 64,
                    )

                self.assertEqual(state.manifest_path.read_bytes(), before)
                stage = _read_manifest(state)["stages"]["script_plan"]
                self.assertEqual(stage["status"], "not_started")
                self.assertEqual(stage["attempt"], 0)

    def test_visual_analysis_failure_does_not_block_script_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            source_root.mkdir()
            with open_pipeline_run(
                brief={"项目名称": "可选视觉项目"},
                cache_dir=root / "cache",
                source_root=source_root,
                cache_fingerprint="optional-visual",
            ) as state:
                for stage_id in ("input_freeze", "source_catalog", "media_manifest"):
                    start_pipeline_stage(state, stage_id, input_sha256=f"in-{stage_id}")
                    finish_pipeline_stage(
                        state,
                        stage_id,
                        StageStatus.SUCCEEDED,
                        output_sha256=f"out-{stage_id}",
                    )
                start_pipeline_stage(state, "visual_analysis", input_sha256="visual-in")
                finish_pipeline_stage(
                    state,
                    "visual_analysis",
                    StageStatus.FAILED,
                    error=StageError(
                        category=StageErrorCategory.EXECUTION,
                        code="visual_provider_failed",
                        message="可选视觉服务失败",
                        retryable=True,
                    ),
                )
                stage = start_pipeline_stage(
                    state,
                    "script_plan",
                    input_sha256="script-in",
                )

            self.assertEqual(stage["status"], "running")

    def test_changed_input_invalidates_only_dependency_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "下游失效项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="invalidate-descendants",
            ) as state:
                _succeed_before(state, "workbook_render")

                start_pipeline_stage(
                    state,
                    "source_catalog",
                    input_sha256="c" * 64,
                )
                stages = _read_manifest(state)["stages"]

                self.assertEqual(stages["source_catalog"]["status"], "running")
                self.assertEqual(stages["source_catalog"]["attempt"], 2)
                self.assertEqual(stages["media_manifest"]["status"], "succeeded")
                self.assertEqual(stages["visual_analysis"]["status"], "succeeded")
                for stage_id in (
                    "script_plan",
                    "script_draft",
                    "visual_binding",
                    "workbook_render",
                    "machine_audit",
                    "human_review",
                    "publish_feedback",
                ):
                    self.assertEqual(stages[stage_id]["status"], "invalidated")
                    self.assertIsNone(stages[stage_id]["output_sha256"])
                    self.assertEqual(stages[stage_id]["output_refs"], [])

    def test_from_stage_invalidates_selected_stage_and_its_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "指定节点恢复项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="from-stage",
            ) as state:
                _succeed_before(state, "script_plan")

            with resume_pipeline_run(
                state.run_id,
                cache_dir,
                from_stage="media_manifest",
            ) as resumed:
                stages = _read_manifest(resumed)["stages"]
                self.assertEqual(stages["source_catalog"]["status"], "succeeded")
                self.assertEqual(stages["script_plan"]["status"], "not_started")
                for stage_id in (
                    "media_manifest",
                    "visual_analysis",
                    "visual_binding",
                    "workbook_render",
                    "machine_audit",
                    "human_review",
                    "publish_feedback",
                ):
                    self.assertEqual(stages[stage_id]["status"], "invalidated")

    def test_waiting_human_review_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "人工复核项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="human-review",
            ) as state:
                _succeed_before(state, "human_review")
                start_pipeline_stage(state, "human_review", input_sha256="a" * 64)
                waiting = finish_pipeline_stage(
                    state,
                    "human_review",
                    StageStatus.WAITING_HUMAN_REVIEW,
                    log_ref="logs://human-review/1",
                )
                self.assertEqual(waiting["status"], "waiting_human_review")
                self.assertIsNone(waiting["error"])

    def test_blocked_cannot_be_converted_to_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "内容阻断项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="content-blocked",
            ) as state:
                _succeed_before(state, "script_plan")
                start_pipeline_stage(state, "script_plan", input_sha256="a" * 64)
                blocked = finish_pipeline_stage(
                    state,
                    "script_plan",
                    StageStatus.BLOCKED,
                    error=StageError(
                        category=StageErrorCategory.INPUT,
                        code="brief_incomplete",
                        message="brief content is incomplete",
                    ),
                )
                with self.assertRaisesRegex(ValueError, "blocked"):
                    finish_pipeline_stage(
                        state,
                        "script_plan",
                        StageStatus.FAILED,
                        error=StageError(
                            category=StageErrorCategory.SYSTEM,
                            code="unexpected_error",
                            message="unexpected technical error",
                        ),
                    )
                self.assertEqual(
                    _read_manifest(state)["stages"]["script_plan"],
                    blocked,
                )

    def test_starting_stage_reopens_unpublished_run_for_finalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "重开运行项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="reopen-run",
            ) as state:
                start_pipeline_stage(state, "input_freeze", input_sha256="a" * 64)
                finish_pipeline_stage(
                    state,
                    "input_freeze",
                    StageStatus.SUCCEEDED,
                    output_sha256="b" * 64,
                )

            self.assertEqual(_read_manifest(state)["status"], "finished")
            with resume_pipeline_run(state.run_id, cache_dir) as resumed:
                start_pipeline_stage(
                    resumed,
                    "input_freeze",
                    input_sha256="c" * 64,
                )
                reopened = _read_manifest(resumed)
                self.assertEqual(reopened["status"], "running")
                self.assertNotIn("finished_at", reopened)
                finalized = finalize_pipeline_run(
                    resumed,
                    status="succeeded",
                    artifacts={},
                )
                self.assertEqual(finalized["status"], "succeeded")

    def test_finalized_bundle_cannot_be_reopened_or_invalidated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "正式发布项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="published-bundle",
            ) as state:
                artifacts = {}
                for name in (
                    "workbook",
                    "audit",
                    "loop_log",
                    "visual_review",
                    "run_report",
                ):
                    path = state.run_dir / f"{name}.dat"
                    path.write_text(name, encoding="utf-8")
                    artifacts[name] = path
                publish_pipeline_run(state, "succeeded", artifacts)

            before = state.manifest_path.read_bytes()
            with resume_pipeline_run(state.run_id, cache_dir) as resumed:
                with self.assertRaisesRegex(ValueError, "finalized bundle"):
                    start_pipeline_stage(
                        resumed,
                        "input_freeze",
                        input_sha256="a" * 64,
                    )
            with self.assertRaisesRegex(ValueError, "finalized bundle"):
                with resume_pipeline_run(
                    state.run_id,
                    cache_dir,
                    from_stage="input_freeze",
                ):
                    self.fail("published bundle must not be invalidated")
            self.assertEqual(state.manifest_path.read_bytes(), before)

    def test_atomic_stage_update_failure_keeps_previous_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "原子状态项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="atomic-stage-state",
            ) as state:
                before = state.manifest_path.read_bytes()
                with patch(
                    "mvp_v2.run_state.os.replace",
                    side_effect=OSError("replace failed"),
                ):
                    with self.assertRaisesRegex(OSError, "replace failed"):
                        start_pipeline_stage(
                            state,
                            "input_freeze",
                            input_sha256="a" * 64,
                        )

                self.assertEqual(state.manifest_path.read_bytes(), before)
                self.assertEqual(
                    list(state.run_dir.glob(f".{state.manifest_path.name}.*.tmp")),
                    [],
                )

    def test_resume_upgrades_legacy_v1_manifest_without_losing_run_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            run_id = "legacy-v1-run"
            manifest_path = cache_dir / "runs" / run_id / "run_manifest.json"
            manifest_path.parent.mkdir(parents=True)
            legacy = {
                "schema_version": "stage1-pipeline-runtime/v1",
                "run_id": run_id,
                "project_name": "旧运行项目",
                "cache_fingerprint": "legacy-cache",
                "status": "failed",
                "started_at": "2026-07-30T00:00:00Z",
                "finished_at": "2026-07-30T00:01:00Z",
            }
            manifest_path.write_text(
                json.dumps(legacy, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with resume_pipeline_run(run_id, cache_dir) as state:
                upgraded = _read_manifest(state)

            self.assertEqual(upgraded["schema_version"], "stage1-pipeline-runtime/v3")
            self.assertEqual(upgraded["source_commit"], configured_source_commit())
            self.assertEqual(upgraded["project_name"], legacy["project_name"])
            self.assertEqual(upgraded["status"], legacy["status"])
            self.assertEqual(
                set(upgraded["stages"]),
                {stage_id.value for stage_id in STAGE_ORDER},
            )


if __name__ == "__main__":
    unittest.main()
