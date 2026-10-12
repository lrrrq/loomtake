import unittest
from unittest.mock import patch

from mvp_v2.knowledge_learning import summarize_revision_experience
from mvp_v2.revision_service import REVISION_REPORT_SCHEMA_VERSION
from mvp_v2.workspace_store import WorkspaceServiceError


def _report(human_status="quality_pass", completion_status="completed"):
    return {
        "schema_version": REVISION_REPORT_SCHEMA_VERSION,
        "project_id": "project_knowledge_learning",
        "parent_version_id": "version_parent_learning",
        "child_version_id": "version_child_learning",
        "original_feedback": "只修改镜 2 文案，结构不要动",
        "actual_impact": {
            "changed_fields": ["copy"],
            "changed_shot_nos": ["2"],
        },
        "locked_field_verification": {
            "checks": [
                {"shot_no": "2", "field": "structure", "status": "preserved"}
            ]
        },
        "rerun_nodes": ["script_draft", "workbook_render"],
        "statuses": {
            "machine_review": {"status": "pass"},
            "human_review": {"status": human_status},
        },
        "completion": {
            "status": completion_status,
            "completed": completion_status == "completed",
        },
    }


class KnowledgeLearningTests(unittest.TestCase):
    def test_verified_revision_becomes_pending_project_candidate_not_rule(self):
        with patch("mvp_v2.text_providers.build_text_provider") as provider:
            result = summarize_revision_experience(
                _report(),
                evidence_refs=["artifact://revision/report/anonymous"],
            )

        provider.assert_not_called()
        self.assertEqual(result["scope"], "project")
        self.assertEqual(result["status"], "pending_approval")
        self.assertFalse(result["active_rule"])
        self.assertFalse(result["company_rule"])
        self.assertEqual(result["provider_calls"], 0)
        self.assertIn("镜 2 文案", result["statement"])
        self.assertIn("镜2:structure", result["statement"])

    def test_unreviewed_or_partial_revision_cannot_be_learned(self):
        for report in (
            _report(human_status="waiting_human_review"),
            _report(completion_status="partially_completed"),
        ):
            with self.subTest(report=report["completion"]["status"]):
                with self.assertRaisesRegex(WorkspaceServiceError, "REVIEW_REQUIRED"):
                    summarize_revision_experience(
                        report,
                        evidence_refs=["artifact://revision/report/anonymous"],
                    )

    def test_evidence_must_be_logical_and_nonempty(self):
        with self.assertRaisesRegex(WorkspaceServiceError, "evidence_refs"):
            summarize_revision_experience(_report(), evidence_refs=[])
        with self.assertRaisesRegex(WorkspaceServiceError, "artifact://"):
            summarize_revision_experience(_report(), evidence_refs=["/tmp/report.json"])


if __name__ == "__main__":
    unittest.main()
