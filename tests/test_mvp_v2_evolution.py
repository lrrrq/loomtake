import json
from pathlib import Path
import tempfile
import unittest

from mvp_v2.evolution import (
    EVOLUTION_LEDGER_NAME,
    record_evolution_event,
    record_rule_candidate_decision,
    summarize_evolution_ledgers,
)


class EvolutionTests(unittest.TestCase):
    def test_cross_project_resolved_and_adopted_feedback_becomes_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledgers = []
            for project_id, suffix in (("project-a", "a"), ("project-b", "b")):
                project = root / project_id
                ledgers.append(project / EVOLUTION_LEDGER_NAME)
                self._record_adopted_feedback(
                    project,
                    feedback_id=f"feedback-{suffix}",
                    parent_run_id=f"parent-{suffix}",
                    child_run_id=f"child-{suffix}",
                    reason="空图，建议补充生活方式画面。" if suffix == "a" else "空图 建议补充生活方式画面",
                )

            summary = summarize_evolution_ledgers(ledgers)

            self.assertEqual(summary["rule_candidate_count"], 1)
            self.assertEqual(summary["pending_rule_candidate_count"], 1)
            candidate = summary["rule_candidates"][0]
            self.assertEqual(candidate["category"], "image")
            self.assertEqual(candidate["occurrence_count"], 2)
            self.assertEqual(candidate["project_count"], 2)
            self.assertEqual(candidate["decision"], "pending")
            self.assertFalse(candidate["automatic_rule_update"])
            self.assertEqual(summary["feedback_resolution"]["resolved_count"], 2)

    def test_unresolved_unadopted_or_single_project_feedback_is_not_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project-a"
            self._record_adopted_feedback(
                project,
                feedback_id="feedback-1",
                parent_run_id="parent-1",
                child_run_id="child-1",
                reason="品牌表达不够克制",
            )
            self._record_adopted_feedback(
                project,
                feedback_id="feedback-2",
                parent_run_id="parent-2",
                child_run_id="child-2",
                reason="品牌表达不够克制",
            )
            other = root / "project-b"
            record_evolution_event(
                other,
                run_id="parent-3",
                event="feedback_received",
                details={
                    "feedback_id": "feedback-3",
                    "category": "style",
                    "reason": "品牌表达不够克制",
                },
            )
            record_evolution_event(
                other,
                run_id="child-3",
                event="feedback_status_changed",
                details={
                    "feedback_id": "feedback-3",
                    "status": "processing",
                    "child_run_id": "child-3",
                },
            )

            summary = summarize_evolution_ledgers([
                project / EVOLUTION_LEDGER_NAME,
                other / EVOLUTION_LEDGER_NAME,
            ])

            self.assertEqual(summary["rule_candidates"], [])
            self.assertEqual(summary["pending_rule_candidate_count"], 0)

    def test_human_decision_is_append_only_and_never_applies_rule_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledgers = []
            for project_id, suffix in (("project-a", "a"), ("project-b", "b")):
                project = root / project_id
                ledgers.append(project / EVOLUTION_LEDGER_NAME)
                self._record_adopted_feedback(
                    project,
                    feedback_id=f"feedback-{suffix}",
                    parent_run_id=f"parent-{suffix}",
                    child_run_id=f"child-{suffix}",
                    reason="空图需要人工确认",
                )
            candidate = summarize_evolution_ledgers(ledgers)["rule_candidates"][0]
            governance = root / "_governance"

            first = record_rule_candidate_decision(
                governance,
                candidate=candidate,
                decision="approved",
                reviewer="测试审核人",
                note="仅批准为规则开发候选，不自动改表",
            )
            second = record_rule_candidate_decision(
                governance,
                candidate=candidate,
                decision="approved",
                reviewer="测试审核人",
                note="仅批准为规则开发候选，不自动改表",
            )
            ledgers.append(governance / EVOLUTION_LEDGER_NAME)
            summary = summarize_evolution_ledgers(ledgers)

            self.assertFalse(first["deduplicated"])
            self.assertTrue(second["deduplicated"])
            self.assertEqual(summary["rule_candidates"][0]["decision"], "approved")
            self.assertEqual(summary["rule_candidates"][0]["reviewer"], "测试审核人")
            self.assertFalse(summary["automatic_rule_update"])
            rows = [
                json.loads(line)
                for line in (governance / EVOLUTION_LEDGER_NAME).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["event"] for row in rows], ["rule_candidate_decision"])
            self.assertFalse(rows[0]["details"]["automatic_rule_update"])

    def test_rule_candidate_decision_requires_eligible_candidate_and_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "审核人"):
                record_rule_candidate_decision(
                    root,
                    candidate={"candidate_id": "rule-1", "eligible": True},
                    decision="approved",
                    reviewer="",
                )
            with self.assertRaisesRegex(ValueError, "跨项目采用证据"):
                record_rule_candidate_decision(
                    root,
                    candidate={"candidate_id": "rule-1", "eligible": False},
                    decision="approved",
                    reviewer="审核人",
                )

    @staticmethod
    def _record_adopted_feedback(
        project: Path,
        *,
        feedback_id: str,
        parent_run_id: str,
        child_run_id: str,
        reason: str,
    ) -> None:
        record_evolution_event(
            project,
            run_id=parent_run_id,
            event="feedback_received",
            details={
                "feedback_id": feedback_id,
                "category": "image",
                "reason": reason,
                "shot_no": "8",
            },
        )
        record_evolution_event(
            project,
            run_id=child_run_id,
            event="feedback_status_changed",
            details={
                "feedback_id": feedback_id,
                "status": "resolved",
                "child_run_id": child_run_id,
            },
        )
        record_evolution_event(
            project,
            run_id=child_run_id,
            event="version_adopted",
            details={"quality_gate": "machine_pass_and_human_quality_pass"},
        )


if __name__ == "__main__":
    unittest.main()
