import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from mvp_v2.pipeline_runtime import open_pipeline_run, publish_pipeline_run
from mvp_v2.pipeline_stages import AutoPipelineStageError
from mvp_v2.review_publish_stages import (
    ReviewAttestationError,
    run_human_review_stage,
    run_publish_feedback_stage,
)
from mvp_v2.run_contract import sha256_file
from mvp_v2.stage_contract import STAGE_ORDER, StageStatus
from mvp_v2.stage_runtime import finish_pipeline_stage, start_pipeline_stage


class ReviewPublishStageTests(unittest.TestCase):
    def test_authorized_independent_ai_review_is_traceable_and_can_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._runtime(root) as state:
                current_target = self._current_target(root)
                audit, workbook, review_html = self._real_inputs(state.run_dir)
                self._complete_through_machine_audit(state, audit, workbook)
                attestation = self._write_attestation(
                    root / "independent-review.json",
                    state,
                    workbook,
                    audit,
                    verdict="quality_pass",
                )

                review = run_human_review_stage(
                    state,
                    machine_audit_path=audit,
                    workbook_path=workbook,
                    review_html_path=review_html,
                    review_attestation_path=attestation,
                )

                self.assertEqual(review.reviewer_kind, "independent_ai")
                self.assertEqual(review.quality_review_status, "quality_pass")
                self.assertEqual(review.human_review_status, "not_performed")
                self.assertEqual(review.stage_status, "succeeded")
                self.assertTrue(Path(review.report["review_attestation_path"]).is_file())

                artifacts = {
                    "workbook": workbook,
                    "audit": audit,
                    "visual_review": review_html,
                }
                for name in ("loop_log", "run_report"):
                    path = state.run_dir / f"{name}.json"
                    path.write_text("{}", encoding="utf-8")
                    artifacts[name] = path

                def publisher(_feedback_path, completion):
                    publish_pipeline_run(
                        state,
                        "succeeded",
                        artifacts,
                        metrics={"reviewer_kind": "independent_ai"},
                        final_stage=completion,
                    )

                feedback = run_publish_feedback_stage(
                    state,
                    machine_audit_path=audit,
                    human_review_report_path=review.report_path,
                    release_artifacts=artifacts,
                    publisher=publisher,
                )

                self.assertEqual(feedback.feedback["reviewer_kind"], "independent_ai")
                self.assertEqual(feedback.feedback["human_review_ref"], "")
                self.assertEqual((root / "cache" / "CURRENT").resolve(), current_target)
                self.assertTrue((state.run_dir / "run_manifest.json").is_file())
                event = self._evolution_events(root)[-1]
                self.assertEqual(event["details"]["reviewer_kind"], "independent_ai")
                self.assertEqual(
                    event["details"]["quality_gate"],
                    "machine_pass_and_authorized_independent_ai_quality_pass",
                )

    def test_independent_ai_review_hash_mismatch_is_rejected_before_stage_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._runtime(root) as state:
                audit, workbook, review_html = self._real_inputs(state.run_dir)
                self._complete_through_machine_audit(state, audit, workbook)
                attestation = self._write_attestation(
                    root / "independent-review.json",
                    state,
                    workbook,
                    audit,
                    verdict="quality_pass",
                )
                payload = json.loads(attestation.read_text(encoding="utf-8"))
                payload["source_workbook_sha256"] = "0" * 64
                attestation.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(ReviewAttestationError, "源工作簿哈希不一致"):
                    run_human_review_stage(
                        state,
                        machine_audit_path=audit,
                        workbook_path=workbook,
                        review_html_path=review_html,
                        review_attestation_path=attestation,
                    )

                record = self._manifest(state)["stages"]["human_review"]
                self.assertEqual(record["status"], "not_started")
                self.assertEqual(record["attempt"], 0)

    def test_independent_ai_needs_revision_is_blocked_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._runtime(root) as state:
                current_target = self._current_target(root)
                audit, workbook, review_html = self._real_inputs(state.run_dir)
                self._complete_through_machine_audit(state, audit, workbook)
                attestation = self._write_attestation(
                    root / "independent-review.json",
                    state,
                    workbook,
                    audit,
                    verdict="needs_revision",
                    blocking_issues=[{"shot": 2, "message": "配图与画面描述不一致"}],
                )

                review = run_human_review_stage(
                    state,
                    machine_audit_path=audit,
                    workbook_path=workbook,
                    review_html_path=review_html,
                    review_attestation_path=attestation,
                )

                self.assertEqual(review.quality_review_status, "quality_fail")
                self.assertEqual(review.human_review_status, "not_performed")
                self.assertEqual(review.stage_status, "blocked")
                stages = self._manifest(state)["stages"]
                self.assertEqual(stages["publish_feedback"]["status"], "not_started")
                self.assertEqual((root / "cache" / "CURRENT").resolve(), current_target)

    def test_pending_waits_without_error_and_does_not_update_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._runtime(root) as state:
                current_target = self._current_target(root)
                audit, workbook, review_html = self._inputs(state.run_dir)
                self._complete_through_machine_audit(state, audit, workbook)
                with patch(
                    "mvp_v2.review_publish_stages.analyze_review_workbooks",
                    return_value=self._review_report("pending"),
                ), patch(
                    "mvp_v2.review_publish_stages._invalid_review_dates",
                    return_value=[],
                ):
                    result = run_human_review_stage(
                        state,
                        machine_audit_path=audit,
                        workbook_path=workbook,
                        review_html_path=review_html,
                    )

                record = self._manifest(state)["stages"]["human_review"]
                self.assertEqual(result.stage_status, "waiting_human_review")
                self.assertEqual(record["status"], "waiting_human_review")
                self.assertIsNone(record["error"])
                self.assertEqual(Path(record["output_refs"][0]), result.report_path.resolve())
                self.assertEqual((root / "cache" / "CURRENT").resolve(), current_target)

    def test_pass_publishes_before_succeeding_and_feedback_is_candidate_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._runtime(root) as state:
                current_target = self._current_target(root)
                audit, workbook, review_html = self._inputs(state.run_dir)
                self._complete_through_machine_audit(state, audit, workbook)
                report = self._review_report(
                    "quality_pass",
                    candidates=[{"candidate": "人工确认后再评估"}],
                )
                with patch(
                    "mvp_v2.review_publish_stages.analyze_review_workbooks",
                    return_value=report,
                ), patch(
                    "mvp_v2.review_publish_stages._invalid_review_dates",
                    return_value=[],
                ):
                    human = run_human_review_stage(
                        state,
                        machine_audit_path=audit,
                        workbook_path=workbook,
                        review_html_path=review_html,
                    )

                artifacts = {
                    "workbook": workbook,
                    "audit": audit,
                    "visual_review": review_html,
                }
                for name in ("loop_log", "run_report"):
                    path = state.run_dir / f"{name}.json"
                    path.write_text("{}", encoding="utf-8")
                    artifacts[name] = path

                def publisher(_feedback_path, completion):
                    publish_pipeline_run(
                        state,
                        "succeeded",
                        artifacts,
                        final_stage=completion,
                    )

                feedback = run_publish_feedback_stage(
                    state,
                    machine_audit_path=audit,
                    human_review_report_path=human.report_path,
                    release_artifacts=artifacts,
                    publisher=publisher,
                )

                stages = self._manifest(state)["stages"]
                self.assertEqual(stages["human_review"]["status"], "succeeded")
                self.assertEqual(stages["publish_feedback"]["status"], "succeeded")
                self.assertEqual(feedback.action, "candidate_review_required")
                self.assertFalse(feedback.feedback["automatic_rule_update"])
                self.assertEqual((root / "cache" / "CURRENT").resolve(), current_target)
                self.assertTrue((state.run_dir / "run_manifest.json").is_file())
                self.assertEqual(
                    [row["event"] for row in self._evolution_events(root)],
                    ["version_adopted"],
                )

    def test_quality_fail_is_contract_blocked_and_publish_does_not_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._runtime(root) as state:
                current_target = self._current_target(root)
                audit, workbook, review_html = self._inputs(state.run_dir)
                self._complete_through_machine_audit(state, audit, workbook)
                with patch(
                    "mvp_v2.review_publish_stages.analyze_review_workbooks",
                    return_value=self._review_report("quality_fail"),
                ), patch(
                    "mvp_v2.review_publish_stages._invalid_review_dates",
                    return_value=[],
                ):
                    human = run_human_review_stage(
                        state,
                        machine_audit_path=audit,
                        workbook_path=workbook,
                        review_html_path=review_html,
                    )

                stages = self._manifest(state)["stages"]
                self.assertEqual(human.stage_status, "blocked")
                self.assertEqual(stages["human_review"]["error"]["category"], "contract")
                self.assertEqual(stages["publish_feedback"]["status"], "not_started")
                self.assertEqual((root / "cache" / "CURRENT").resolve(), current_target)

    def test_publish_failure_marks_stage_failed_and_keeps_old_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._runtime(root) as state:
                current_target = self._current_target(root)
                audit, workbook, review_html = self._inputs(state.run_dir)
                self._complete_through_machine_audit(state, audit, workbook)
                with patch(
                    "mvp_v2.review_publish_stages.analyze_review_workbooks",
                    return_value=self._review_report("quality_pass"),
                ), patch(
                    "mvp_v2.review_publish_stages._invalid_review_dates",
                    return_value=[],
                ):
                    human = run_human_review_stage(
                        state,
                        machine_audit_path=audit,
                        workbook_path=workbook,
                        review_html_path=review_html,
                    )

                with self.assertRaises(AutoPipelineStageError):
                    run_publish_feedback_stage(
                        state,
                        machine_audit_path=audit,
                        human_review_report_path=human.report_path,
                        release_artifacts={
                            "workbook": workbook,
                            "audit": audit,
                            "visual_review": review_html,
                        },
                        publisher=lambda _feedback, _completion: (_ for _ in ()).throw(
                            RuntimeError("injected publish failure")
                        ),
                    )

                record = self._manifest(state)["stages"]["publish_feedback"]
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["error"]["category"], "system")
                self.assertTrue(record["error"]["retryable"])
                self.assertEqual((root / "cache" / "CURRENT").resolve(), current_target)
                self.assertEqual(self._evolution_events(root), [])

    def test_legacy_current_writer_is_not_called_by_publish_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._runtime(root) as state:
                current_target = self._current_target(root)
                audit, workbook, review_html = self._inputs(state.run_dir)
                self._complete_through_machine_audit(state, audit, workbook)
                with patch(
                    "mvp_v2.review_publish_stages.analyze_review_workbooks",
                    return_value=self._review_report("quality_pass"),
                ), patch(
                    "mvp_v2.review_publish_stages._invalid_review_dates",
                    return_value=[],
                ):
                    human = run_human_review_stage(
                        state,
                        machine_audit_path=audit,
                        workbook_path=workbook,
                        review_html_path=review_html,
                    )
                loop_log = state.run_dir / "loop.json"
                loop_log.write_text("loop", encoding="utf-8")
                run_report = state.run_dir / "run_report.json"
                run_report.write_text("{}", encoding="utf-8")
                artifacts = {
                    "workbook": workbook,
                    "audit": audit,
                    "loop_log": loop_log,
                    "visual_review": review_html,
                    "run_report": run_report,
                }

                def publisher(_feedback_path, completion):
                    publish_pipeline_run(
                        state,
                        "succeeded",
                        artifacts,
                        final_stage=completion,
                    )

                run_publish_feedback_stage(
                    state,
                    machine_audit_path=audit,
                    human_review_report_path=human.report_path,
                    release_artifacts=artifacts,
                    publisher=publisher,
                )

                manifest = self._manifest(state)
                self.assertEqual(manifest["status"], "succeeded")
                self.assertTrue(manifest["bundle_finalized"])
                self.assertEqual(manifest["stages"]["publish_feedback"]["status"], "succeeded")
                self.assertEqual((root / "cache" / "CURRENT").resolve(), current_target)
                self.assertEqual([row["event"] for row in self._evolution_events(root)], ["version_adopted"])

    def test_review_date_is_optional_and_never_blocks_a_valid_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._runtime(root) as state:
                audit, workbook, review_html = self._inputs(state.run_dir)
                self._complete_through_machine_audit(state, audit, workbook)
                with patch(
                    "mvp_v2.review_publish_stages.analyze_review_workbooks",
                    return_value=self._review_report("quality_pass"),
                ), patch(
                    "mvp_v2.review_publish_stages._invalid_review_dates",
                    return_value=["剪辑复核!审核日期2 无效"],
                ):
                    result = run_human_review_stage(
                        state,
                        machine_audit_path=audit,
                        workbook_path=workbook,
                        review_html_path=review_html,
                    )

                record = self._manifest(state)["stages"]["human_review"]
                self.assertEqual(result.human_review_status, "quality_pass")
                self.assertEqual(record["status"], "succeeded")
                self.assertIsNone(record["error"])

    @staticmethod
    def _runtime(root: Path):
        return open_pipeline_run(
            brief={"项目名称": "匿名发布会"},
            cache_dir=root / "cache",
            source_root=root / "source",
            cache_fingerprint="review-publish-stage",
        )

    @staticmethod
    def _evolution_events(root: Path):
        path = root / "cache" / "evolution_ledger.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def _current_target(root: Path) -> Path:
        old_run = root / "cache" / "runs" / "old-release"
        old_run.mkdir(parents=True)
        current = root / "cache" / "CURRENT"
        current.symlink_to(Path("runs") / old_run.name)
        return old_run.resolve()

    @staticmethod
    def _inputs(run_dir: Path):
        audit = run_dir / "audit.json"
        audit.write_text(json.dumps({
            "status": "pass",
            "warning_count": 0,
            "hard_fail_count": 0,
        }), encoding="utf-8")
        workbook = run_dir / "review.xlsx"
        workbook.write_bytes(b"review-workbook")
        review_html = run_dir / "review.html"
        review_html.write_text("review", encoding="utf-8")
        return audit, workbook, review_html

    @staticmethod
    def _real_inputs(run_dir: Path):
        audit = run_dir / "audit.json"
        audit.write_text(json.dumps({
            "status": "pass",
            "warning_count": 0,
            "hard_fail_count": 0,
        }), encoding="utf-8")
        workbook = run_dir / "review.xlsx"
        book = Workbook()
        review = book.active
        review.title = "剪辑复核"
        review.append(["项目名称", "镜号", "画面描述"])
        review.append(["匿名发布会", 1, "开场"])
        review.append(["匿名发布会", 2, "收束"])
        adjacent = book.create_sheet("相邻镜头复核")
        adjacent.append(["项目名称", "边"])
        adjacent.append(["匿名发布会", "1→2"])
        full_film = book.create_sheet("整片复核")
        full_film.append(["项目名称"])
        full_film.append(["匿名发布会"])
        details = book.create_sheet("抽调明细")
        details.append(["采用来源文件", "采用来源 sheet", "采用来源行"])
        details.append(["来源.xlsx", "模板", 2])
        details.append(["来源.xlsx", "创意库", 3])
        book.save(workbook)
        book.close()
        review_html = run_dir / "review.html"
        review_html.write_text("review", encoding="utf-8")
        return audit, workbook, review_html

    @staticmethod
    def _write_attestation(
        path: Path,
        state,
        workbook: Path,
        audit: Path,
        *,
        verdict: str,
        blocking_issues=None,
    ) -> Path:
        payload = {
            "schema_version": "mplus-quality-review-attestation/v1",
            "reviewer_kind": "independent_ai",
            "reviewer_thread_id": "019fea28-5cad-7803-bbc7-b71e55a48250",
            "reviewer_output_sha256": "a" * 64,
            "reviewed_at": "2026-08-10T10:00:00Z",
            "user_authorization": {
                "authorized": True,
                "authorized_at": "2026-08-10",
                "scope": "允许独立新对话 AI 代替大量操作性复核，不冒充真人",
            },
            "run_id": state.run_id,
            "project_name": "匿名发布会",
            "source_workbook_sha256": sha256_file(workbook),
            "machine_audit_sha256": sha256_file(audit),
            "source_manifest_sha256": sha256_file(state.manifest_path),
            "verdict": verdict,
            "counts": {
                "shot_count": 2,
                "adjacent_edge_count": 1,
                "full_film_count": 1,
                "source_triplet_count": 2,
            },
            "blocking_issues": list(blocking_issues or []),
            "warnings": [],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _complete_through_machine_audit(state, audit: Path, workbook: Path) -> None:
        refs = {
            "workbook_render": (str(workbook.resolve()),),
            "machine_audit": (str(workbook.resolve()), str(audit.resolve())),
        }
        for stage_id in STAGE_ORDER:
            if stage_id.value in {"human_review", "publish_feedback"}:
                break
            start_pipeline_stage(
                state,
                stage_id,
                input_sha256=f"input-{stage_id.value}",
                input_refs=(),
            )
            finish_pipeline_stage(
                state,
                stage_id,
                StageStatus.SUCCEEDED,
                output_sha256=f"output-{stage_id.value}",
                output_refs=refs.get(stage_id.value, ()),
            )

    @staticmethod
    def _review_report(status: str, candidates=None) -> dict:
        return {
            "schema_version": "v0.2.1",
            "status": "pending_review" if status == "pending" else "complete",
            "human_review_status": status,
            "projects": [],
            "summary": {},
            "rule_change_candidates": list(candidates or []),
        }

    @staticmethod
    def _manifest(state) -> dict:
        return json.loads(state.manifest_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
