"""Business review actions do not require users to retype their decision."""

import unittest

from mvp_v2.formal_console import ReviewDecisionPayload, ReviewFinalPayload
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_review_workspace as review_fixture


class ReviewUsabilityTests(unittest.TestCase):
    def test_explicit_approve_and_final_payloads_allow_optional_comment(self):
        approve = ReviewDecisionPayload(
            expected_project_version=1, idempotency_key="approve-selected",
            issue_keys=["review_one", "review_two"], decision="approve",
        )
        final = ReviewFinalPayload(
            expected_project_version=1, idempotency_key="confirm-final", confirmed=True,
        )
        self.assertEqual(approve.note, "")
        self.assertEqual(final.note, "")

    def test_batch_approval_keeps_per_item_evidence_and_return_requires_reason(self):
        fixture = review_fixture.FormalReviewWorkspaceTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        current = fixture._start()
        ordinary = [item for item in current["issues"] if item["kind"] == "image"]
        self.assertGreater(len(ordinary), 1)
        self.assertTrue(all(item["focus_required"] is False for item in ordinary))
        changed = [item for item in current["issues"] if item["kind"] == "change"]
        self.assertTrue(all(item["focus_required"] for item in changed))
        with self.assertRaises(WorkspaceServiceError) as rejected:
            fixture.review.decide(
                fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
                expected_project_version=current["project_version"],
                issue_keys=(ordinary[0]["issue_key"],), decision="return", note=" ",
                idempotency_key="empty-return",
            )
        self.assertEqual(rejected.exception.code, "INVALID_INPUT")
        approved = fixture.review.decide(
            fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
            expected_project_version=current["project_version"],
            issue_keys=tuple(item["issue_key"] for item in current["issues"]),
            decision="approve", note="", idempotency_key="explicit-approve-all",
        )
        self.assertTrue(approved["ready_for_final"])
        self.assertEqual(len(approved["issues"]), len(current["issues"]))
        self.assertTrue(all(item["status"] == "approve" for item in approved["issues"]))
        final = fixture.review.confirm_final(
            fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
            expected_project_version=approved["project_version"], note="",
            idempotency_key="explicit-final",
        )
        self.assertEqual(final["state"], "confirmed")
        self.assertGreater(final["decision"]["approved_count"], 1)
        restarted = fixture._review_service(fixture.store).read_current(
            fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
        )
        self.assertEqual(restarted["state"], "confirmed")
