import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp_v2.pipeline_runtime import (
    PipelineRunState,
    finalize_pipeline_run,
    fork_pipeline_run,
    open_pipeline_run,
    publish_file_atomic,
    publish_pipeline_run,
    migrate_legacy_release,
    resume_pipeline_run,
    resolve_current_release,
    resolve_pipeline_run,
)
from mvp_v2.runtime_config import configured_source_commit


def _sha256_json(value):
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PipelineRuntimeTests(unittest.TestCase):
    def test_resume_rejects_source_commit_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()
            with patch.dict(os.environ, {"MPLUS_SOURCE_COMMIT": "a" * 40}, clear=False):
                with open_pipeline_run(
                    brief={"项目名称": "提交来源测试"},
                    cache_dir=cache_dir,
                    source_root=source_root,
                    cache_fingerprint="source-commit",
                ) as state:
                    run_id = state.run_id

            with patch.dict(os.environ, {"MPLUS_SOURCE_COMMIT": "b" * 40}, clear=False):
                with self.assertRaisesRegex(ValueError, "运行源提交已变化"):
                    with resume_pipeline_run(run_id, cache_dir):
                        self.fail("different source commit must not resume an existing run")

    def test_migrate_legacy_release_preserves_orphan_run_report_without_blocking_first_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            cache_dir.mkdir()
            public_paths = {
                "workbook": root / "public.xlsx",
                "audit": root / "public.audit.json",
                "loop_log": root / "public.loop.json",
                "visual_review": root / "public.visual_review.html",
                "run_report": cache_dir / "run_report.json",
            }
            public_paths["run_report"].write_text("legacy report", encoding="utf-8")
            artifact_paths = {}
            run_dir = cache_dir / "runs" / "new-run"
            run_dir.mkdir(parents=True)
            for name in public_paths:
                path = run_dir / f"{name}.dat"
                path.write_text(name, encoding="utf-8")
                artifact_paths[name] = path

            migrated = migrate_legacy_release(cache_dir, public_paths, artifact_paths)

            self.assertEqual(set(migrated or {}), {"run_report"})
            backup = migrated["run_report"]
            self.assertEqual(backup.read_text(encoding="utf-8"), "legacy report")
            self.assertTrue(public_paths["run_report"].is_file())
            self.assertFalse((cache_dir / "CURRENT").exists())

    def test_migrate_legacy_release_still_rejects_incomplete_customer_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            cache_dir.mkdir()
            public_paths = {
                "workbook": root / "public.xlsx",
                "audit": root / "public.audit.json",
                "loop_log": root / "public.loop.json",
                "visual_review": root / "public.visual_review.html",
                "run_report": cache_dir / "run_report.json",
            }
            public_paths["workbook"].write_bytes(b"legacy workbook")
            artifact_paths = {name: root / f"new-{name}.dat" for name in public_paths}

            with self.assertRaisesRegex(RuntimeError, "旧正式文件不完整"):
                migrate_legacy_release(cache_dir, public_paths, artifact_paths)

    def test_legacy_migration_is_stable_readable_and_never_sets_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            public_paths = {
                "workbook": root / "formal" / "result.xlsx",
                "audit": root / "formal" / "result.xlsx.audit.json",
                "loop_log": root / "formal" / "result.xlsx.loop.json",
                "visual_review": root / "formal" / "result.visual_review.html",
                "run_report": cache_dir / "run_report.json",
            }
            for name, path in public_paths.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"anonymous legacy {name}".encode("utf-8"))
            artifact_paths = {
                name: root / "new-run" / f"{name}.dat"
                for name in public_paths
            }

            first = migrate_legacy_release(cache_dir, public_paths, artifact_paths)
            second = migrate_legacy_release(cache_dir, public_paths, artifact_paths)

            self.assertIsNotNone(first)
            self.assertEqual(first, second)
            run_dir = next(iter(first.values())).parent
            self.assertTrue(run_dir.name.startswith("legacy-"))
            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            migration = json.loads((run_dir / "legacy_migration.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "legacy")
            self.assertEqual(migration, manifest["legacy_migration"])
            self.assertFalse((cache_dir / "CURRENT").exists())
            self.assertFalse((cache_dir / "CURRENT").is_symlink())
            self.assertFalse((cache_dir / "workspace.sqlite3").exists())

    def test_legacy_migration_ignores_valid_current_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            public_paths, artifact_paths = self._legacy_migration_paths(root, cache_dir)
            with open_pipeline_run(
                {"项目名称": "现有匿名运行"}, cache_dir, root, "fixture"
            ) as state:
                current_artifacts = {}
                for name in public_paths:
                    path = state.run_dir / f"current-{name}.fixture"
                    path.write_bytes(f"current {name}".encode("utf-8"))
                    current_artifacts[name] = path
                publish_pipeline_run(state, "completed", current_artifacts)
                current = cache_dir / "CURRENT"
                current.symlink_to(Path("runs") / state.run_id)
                self.assertEqual(
                    set(resolve_current_release(cache_dir)),
                    set(current_artifacts),
                )

            with patch(
                "mvp_v2.pipeline_runtime.resolve_current_release",
                side_effect=AssertionError("legacy migration 不得读取 CURRENT"),
            ) as current_reader:
                migrated = migrate_legacy_release(cache_dir, public_paths, artifact_paths)

            current_reader.assert_not_called()
            self.assertIsNotNone(migrated)
            self.assertTrue(current.is_symlink())
            self.assertEqual(current.resolve().name, state.run_id)
            self.assertTrue(next(iter(migrated.values())).parent.name.startswith("legacy-"))

    def test_legacy_migration_ignores_damaged_current_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            public_paths, artifact_paths = self._legacy_migration_paths(root, cache_dir)
            current = cache_dir / "CURRENT"
            current.parent.mkdir(parents=True, exist_ok=True)
            current.symlink_to(Path("runs") / "missing-run")

            with patch(
                "mvp_v2.pipeline_runtime.resolve_current_release",
                side_effect=AssertionError("legacy migration 不得读取 CURRENT"),
            ) as current_reader:
                migrated = migrate_legacy_release(cache_dir, public_paths, artifact_paths)

            current_reader.assert_not_called()
            self.assertIsNotNone(migrated)
            self.assertTrue(current.is_symlink())
            self.assertEqual(current.readlink(), Path("runs") / "missing-run")
            self.assertTrue(next(iter(migrated.values())).parent.name.startswith("legacy-"))

    @staticmethod
    def _legacy_migration_paths(root: Path, cache_dir: Path) -> tuple[dict[str, Path], dict[str, Path]]:
        public_paths = {
            "workbook": root / "formal" / "result.xlsx",
            "audit": root / "formal" / "result.xlsx.audit.json",
            "loop_log": root / "formal" / "result.xlsx.loop.json",
            "visual_review": root / "formal" / "result.visual_review.html",
            "run_report": cache_dir / "run_report.json",
        }
        for name, path in public_paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"anonymous legacy {name}".encode("utf-8"))
        artifact_paths = {
            name: root / "new-run" / f"{name}.dat"
            for name in public_paths
        }
        return public_paths, artifact_paths

    def test_fork_ignores_excel_input_row_but_preserves_old_manifest_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()
            (source_root / "M+发布会类通用脚本库.xlsx").write_bytes(b"template")
            parent_brief = {"项目名称": "行号兼容", "input_row": 2}
            with open_pipeline_run(
                brief=parent_brief,
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="fp",
            ) as parent_state:
                manifest = json.loads(parent_state.manifest_path.read_text(encoding="utf-8"))
                manifest["brief_spec_sha256"] = _sha256_json(parent_brief)
                parent_state.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                (parent_state.run_dir / "input_freeze.json").write_text(
                    json.dumps({"brief": parent_brief}, ensure_ascii=False),
                    encoding="utf-8",
                )
                parent_id = parent_state.run_id

            with fork_pipeline_run(
                parent_id,
                cache_dir,
                brief={"项目名称": "行号兼容", "input_row": 1},
                source_root=source_root,
                cache_fingerprint="fp",
                from_stage="input_freeze",
            ) as child_state:
                child = json.loads(child_state.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(child["parent_run_id"], parent_id)

    def test_fork_pipeline_run_copies_only_upstream_stage_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()
            template = source_root / "M+发布会类通用脚本库.xlsx"
            template.write_bytes(b"template")
            brief = {"项目名称": "派生版本"}
            with open_pipeline_run(
                brief=brief,
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="fp",
            ) as parent_state:
                manifest = json.loads(parent_state.manifest_path.read_text(encoding="utf-8"))
                for stage_name in ("input_freeze", "source_catalog", "media_manifest", "visual_analysis", "script_plan"):
                    output = parent_state.run_dir / f"{stage_name}.json"
                    output.write_text(stage_name, encoding="utf-8")
                    record = manifest["stages"][stage_name]
                    record.update({
                        "status": "succeeded",
                        "input_sha256": f"in-{stage_name}",
                        "output_sha256": _sha256_file(output),
                        "output_refs": [str(output.resolve())],
                        "attempt": 1,
                    })
                parent_state.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                parent_id = parent_state.run_id

            with fork_pipeline_run(
                parent_id,
                cache_dir,
                brief=brief,
                source_root=source_root,
                cache_fingerprint="fp",
                from_stage="script_draft",
            ) as child_state:
                child = json.loads(child_state.manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(child["parent_run_id"], parent_id)
                self.assertEqual(child["stages"]["script_plan"]["reused_from_run"], parent_id)
                self.assertEqual(child["stages"]["script_draft"]["status"], "not_started")
                copied = Path(child["stages"]["script_plan"]["output_refs"][0])
                self.assertEqual(copied.parent.resolve(), child_state.run_dir.resolve())
                self.assertEqual(copied.read_text(encoding="utf-8"), "script_plan")
    def test_open_pipeline_run_creates_running_manifest_and_holds_lock_until_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()
            template_path = source_root / "模板.xlsx"
            template_path.write_bytes(b"template-binary")
            brief = {
                "项目名称": "测试发布会",
                "模板工作簿路径": "模板.xlsx",
                "MINIMAX_API_KEY": "minimax-secret",
            }

            with open_pipeline_run(
                brief=brief,
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="fingerprint-v1",
            ) as state:
                self.assertIsInstance(state, PipelineRunState)
                self.assertEqual(state.run_dir, cache_dir / "runs" / state.run_id)
                self.assertTrue(state.run_dir.is_dir())
                self.assertEqual(state.manifest_path, state.run_dir / "run_manifest.json")
                self.assertEqual(state.ledger_path, cache_dir / "run_ledger.jsonl")
                self.assertEqual(state.lock_path, cache_dir / ".pipeline.lock")
                self.assertEqual(state.cache_fingerprint, "fingerprint-v1")

                manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(manifest["schema_version"], "stage1-pipeline-runtime/v3")
                self.assertEqual(manifest["source_commit"], configured_source_commit())
                self.assertEqual(manifest["run_id"], state.run_id)
                self.assertEqual(manifest["project_name"], "测试发布会")
                self.assertEqual(
                    manifest["brief_spec_sha256"],
                    _sha256_json({
                        "项目名称": "测试发布会",
                        "模板工作簿路径": "模板.xlsx",
                        "MINIMAX_API_KEY": "[REDACTED]",
                    }),
                )
                self.assertEqual(manifest["cache_fingerprint"], "fingerprint-v1")
                self.assertEqual(manifest["status"], "running")
                self.assertEqual(
                    manifest["template_workbook"],
                    {
                        "path": str(template_path.resolve()),
                        "sha256": _sha256_file(template_path),
                    },
                )
                self.assertNotIn("minimax-secret", state.manifest_path.read_text(encoding="utf-8"))

                with self.assertRaisesRegex(RuntimeError, "锁已被占用"):
                    with open_pipeline_run(
                        brief=brief,
                        cache_dir=cache_dir,
                        source_root=source_root,
                        cache_fingerprint="fingerprint-v1",
                    ):
                        self.fail("second run should be rejected while lock is held")

            ledger_rows = [
                json.loads(line)
                for line in state.ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["event"] for row in ledger_rows], ["started", "finished"])
            self.assertEqual(
                json.loads(state.manifest_path.read_text(encoding="utf-8"))["status"],
                "finished",
            )

            with open_pipeline_run(
                brief=brief,
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="fingerprint-v1",
            ) as reopened:
                self.assertTrue(reopened.lock_path.exists())

    def test_open_pipeline_run_hashes_default_launch_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()
            template_path = source_root / "M+发布会类通用脚本库.xlsx"
            template_path.write_bytes(b"default-launch-template")

            with open_pipeline_run(
                brief={"项目名称": "默认模板项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="fingerprint-default-template",
            ) as state:
                manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(
                manifest["template_workbook"],
                {
                    "path": str(template_path.resolve()),
                    "sha256": _sha256_file(template_path),
                },
            )

    def test_open_pipeline_run_hashes_recursively_redacted_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()
            first_brief = {
                "项目名称": "脱敏哈希项目",
                "MINIMAX_API_KEY": "first-top-level-secret",
                "provider": {
                    "accessToken": "first-nested-secret",
                    "model": "stable-model",
                },
            }
            second_brief = {
                "项目名称": "脱敏哈希项目",
                "MINIMAX_API_KEY": "second-top-level-secret",
                "provider": {
                    "accessToken": "second-nested-secret",
                    "model": "stable-model",
                },
            }
            redacted_brief = {
                "项目名称": "脱敏哈希项目",
                "MINIMAX_API_KEY": "[REDACTED]",
                "provider": {
                    "accessToken": "[REDACTED]",
                    "model": "stable-model",
                },
            }

            manifest_hashes = []
            for brief in (first_brief, second_brief):
                with open_pipeline_run(
                    brief=brief,
                    cache_dir=cache_dir,
                    source_root=source_root,
                    cache_fingerprint="fingerprint-redacted-brief",
                ) as state:
                    manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
                    manifest_hashes.append(manifest["brief_spec_sha256"])

            persisted_text = "\n".join(
                [
                    *(path.read_text(encoding="utf-8") for path in cache_dir.glob("runs/*/run_manifest.json")),
                    (cache_dir / "run_ledger.jsonl").read_text(encoding="utf-8"),
                ]
            )
            self.assertEqual(manifest_hashes, [_sha256_json(redacted_brief)] * 2)
            self.assertNotIn(_sha256_json(first_brief), persisted_text)
            self.assertNotIn(_sha256_json(second_brief), persisted_text)
            for secret in (
                "first-top-level-secret",
                "first-nested-secret",
                "second-top-level-secret",
                "second-nested-secret",
            ):
                self.assertNotIn(secret, persisted_text)

    def test_finalize_pipeline_run_records_artifacts_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()
            brief = {"项目名称": "测试发布会"}
            existing_artifact = root / "artifacts" / "result.xlsx"
            existing_artifact.parent.mkdir()
            existing_artifact.write_bytes(b"artifact-data")
            missing_artifact = root / "artifacts" / "missing.xlsx"

            with open_pipeline_run(
                brief=brief,
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="fingerprint-v2",
            ) as state:
                manifest = finalize_pipeline_run(
                    state,
                    status="succeeded",
                    artifacts={
                        "workbook": existing_artifact,
                        "preview": missing_artifact,
                    },
                    metrics={"shot_count": 12},
                )
                manifest_again = finalize_pipeline_run(
                    state,
                    status="failed",
                    artifacts={"ignored": root / "ignored.txt"},
                    metrics={"ignored": True},
                )

                self.assertEqual(manifest_again, manifest)

            persisted = json.loads(state.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "succeeded")
            self.assertEqual(persisted["metrics"], {"shot_count": 12})
            self.assertEqual(
                persisted["artifacts"]["workbook"],
                {
                    "path": str(existing_artifact.resolve()),
                    "sha256": _sha256_file(existing_artifact),
                    "size_bytes": existing_artifact.stat().st_size,
                },
            )
            self.assertEqual(
                persisted["artifacts"]["preview"],
                {
                    "path": str(missing_artifact.resolve()),
                    "missing": True,
                },
            )

            ledger_rows = [
                json.loads(line)
                for line in state.ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["event"] for row in ledger_rows], ["started", "succeeded"])
            self.assertEqual(len(ledger_rows), 2)

    def test_finalize_pipeline_run_is_idempotent_across_rebuilt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "恢复运行项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="fingerprint-rebuilt-state",
            ) as state:
                first_rebuilt_state = PipelineRunState(
                    run_id=state.run_id,
                    run_dir=state.run_dir,
                    manifest_path=state.manifest_path,
                    ledger_path=state.ledger_path,
                    lock_path=state.lock_path,
                    cache_fingerprint=state.cache_fingerprint,
                )
                first_manifest = finalize_pipeline_run(
                    first_rebuilt_state,
                    status="succeeded",
                    artifacts={},
                )
                second_rebuilt_state = PipelineRunState(
                    run_id=state.run_id,
                    run_dir=state.run_dir,
                    manifest_path=state.manifest_path,
                    ledger_path=state.ledger_path,
                    lock_path=state.lock_path,
                    cache_fingerprint=state.cache_fingerprint,
                )
                second_manifest = finalize_pipeline_run(
                    second_rebuilt_state,
                    status="failed",
                    artifacts={"ignored": root / "ignored.txt"},
                )

            ledger_rows = [
                json.loads(line)
                for line in state.ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(second_manifest, first_manifest)
            self.assertEqual(second_manifest["status"], "succeeded")
            self.assertEqual([row["event"] for row in ledger_rows], ["started", "succeeded"])

    def test_open_pipeline_run_records_failed_manifest_when_context_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()
            brief = {"项目名称": "异常项目"}

            with self.assertRaisesRegex(RuntimeError, "boom"):
                with open_pipeline_run(
                    brief=brief,
                    cache_dir=cache_dir,
                    source_root=source_root,
                    cache_fingerprint="fingerprint-v3",
                ) as state:
                    raise RuntimeError("boom")

            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
            ledger_rows = [
                json.loads(line)
                for line in state.ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual([row["event"] for row in ledger_rows], ["started", "failed"])

            with open_pipeline_run(
                brief=brief,
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="fingerprint-v3",
            ):
                pass

    def test_context_exception_overrides_prior_finalize_with_failed_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with self.assertRaisesRegex(RuntimeError, "after finalize"):
                with open_pipeline_run(
                    brief={"项目名称": "终态覆盖项目"},
                    cache_dir=cache_dir,
                    source_root=source_root,
                    cache_fingerprint="fingerprint-terminal-override",
                ) as state:
                    finalize_pipeline_run(
                        state,
                        status="succeeded",
                        artifacts={},
                    )
                    raise RuntimeError("after finalize")

            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
            ledger_rows = [
                json.loads(line)
                for line in state.ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(
                [row["event"] for row in ledger_rows],
                ["started", "succeeded", "failed"],
            )
            self.assertEqual(ledger_rows[-1]["details"], {"supersedes": "succeeded"})

    def test_context_exception_after_published_release_does_not_mutate_terminal_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with self.assertRaisesRegex(RuntimeError, "after publish"):
                with open_pipeline_run(
                    brief={"项目名称": "发布后异常项目"},
                    cache_dir=cache_dir,
                    source_root=source_root,
                    cache_fingerprint="fingerprint-published-terminal",
                ) as state:
                    artifacts = {}
                    for name in ("workbook", "audit", "loop_log", "visual_review", "run_report"):
                        path = state.run_dir / f"{name}.dat"
                        path.write_text(name, encoding="utf-8")
                        artifacts[name] = path
                    publish_pipeline_run(state, "blocked", artifacts)
                    raise RuntimeError("after publish")

            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
            ledger_rows = [
                json.loads(line)
                for line in state.ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(manifest["status"], "blocked")
            self.assertEqual([row["event"] for row in ledger_rows], ["started", "blocked"])
            self.assertFalse((cache_dir / "CURRENT").exists())
            self.assertEqual(
                set(resolve_pipeline_run(cache_dir, state.run_id, allowed_statuses={"blocked"})),
                set(artifacts),
            )

    def test_resolve_explicit_pipeline_run_rejects_artifact_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "篡改检测项目"},
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="fingerprint-tamper",
            ) as state:
                artifacts = {}
                for name in ("workbook", "audit", "loop_log", "visual_review", "run_report"):
                    path = state.run_dir / f"{name}.dat"
                    path.write_text(name, encoding="utf-8")
                    artifacts[name] = path
                publish_pipeline_run(state, "blocked", artifacts)

            artifacts["run_report"].write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "完整性"):
                resolve_pipeline_run(cache_dir, state.run_id, allowed_statuses={"blocked"})

    def test_resolve_explicit_pipeline_runs_keep_independent_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()

            published = []
            for version in ("old", "new"):
                with open_pipeline_run(
                    brief={"项目名称": f"{version}-release"},
                    cache_dir=cache_dir,
                    source_root=source_root,
                    cache_fingerprint=f"fingerprint-{version}",
                ) as state:
                    artifacts = {}
                    for name in ("workbook", "audit", "loop_log", "visual_review", "run_report"):
                        path = state.run_dir / f"{name}.dat"
                        path.write_text(f"{version}:{name}", encoding="utf-8")
                        artifacts[name] = path
                    publish_pipeline_run(state, "blocked", artifacts)
                    published.append((state.run_dir.resolve(), artifacts))
            old_snapshot = resolve_pipeline_run(cache_dir, published[0][0].name, allowed_statuses={"blocked"})
            current_snapshot = resolve_pipeline_run(cache_dir, published[1][0].name, allowed_statuses={"blocked"})
            self.assertEqual({path.parent for path in old_snapshot.values()}, {published[0][0]})
            self.assertEqual({path.parent for path in current_snapshot.values()}, {published[1][0]})
            self.assertEqual(
                {name: path.read_text(encoding="utf-8") for name, path in old_snapshot.items()},
                {name: f"old:{name}" for name in old_snapshot},
            )
            self.assertEqual(
                {name: path.read_text(encoding="utf-8") for name, path in current_snapshot.items()},
                {name: f"new:{name}" for name in current_snapshot},
            )

    def test_publish_file_atomic_replaces_target_from_same_directory_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.txt"
            target = root / "nested" / "target.txt"
            source.write_text("new-content", encoding="utf-8")
            target.parent.mkdir()
            target.write_text("old-content", encoding="utf-8")
            original_replace = os.replace
            replace_calls = []

            def observe_replace(temp_path, final_target):
                temp_path = Path(temp_path)
                final_target = Path(final_target)
                replace_calls.append({
                    "temp_parent": temp_path.parent,
                    "target_parent": final_target.parent,
                    "temp_content": temp_path.read_text(encoding="utf-8"),
                })
                return original_replace(temp_path, final_target)

            with patch("mvp_v2.pipeline_runtime.os.replace", side_effect=observe_replace):
                publish_file_atomic(source, target)

            self.assertEqual(target.read_text(encoding="utf-8"), "new-content")
            self.assertEqual(len(replace_calls), 1)
            self.assertEqual(replace_calls[0]["temp_parent"], target.parent)
            self.assertEqual(replace_calls[0]["target_parent"], target.parent)
            self.assertEqual(replace_calls[0]["temp_content"], "new-content")
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_publish_file_atomic_keeps_existing_target_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.txt"
            target = root / "nested" / "target.txt"
            source.write_text("new-content", encoding="utf-8")
            target.parent.mkdir()
            target.write_text("old-content", encoding="utf-8")

            with patch("mvp_v2.pipeline_runtime.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    publish_file_atomic(source, target)

            self.assertEqual(target.read_text(encoding="utf-8"), "old-content")
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
