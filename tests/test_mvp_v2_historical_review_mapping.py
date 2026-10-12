from __future__ import annotations

import unittest

from mvp_v2.local_ui import map_historical_review_returns
from mvp_v2.workspace_store import WorkspaceServiceError


class HistoricalReviewMappingTests(unittest.TestCase):
    def setUp(self):
        self.review_issues = [
            {"issue_key": "image-1", "shot_no": "1"},
            {"issue_key": "change-1", "shot_no": "1"},
            {"issue_key": "image-2", "shot_no": "2"},
            {"issue_key": "image-3", "shot_no": "3"},
        ]

    def test_single_shot_failure_returns_every_authoritative_issue_for_that_shot(self):
        result = map_historical_review_returns(
            [{"slot_index": 1, "shot_no": "1", "status": "需修改"}],
            self.review_issues,
        )
        self.assertEqual(result["issue_keys"], ("image-1", "change-1"))
        self.assertEqual(result["shot_nos"], ("1",))

    def test_adjacent_failure_returns_both_edge_endpoints(self):
        result = map_historical_review_returns(
            [{"slot_index": "adjacent:1->2", "shot_no": "1->2", "status": "需修改"}],
            self.review_issues,
        )
        self.assertEqual(
            result["issue_keys"],
            ("image-1", "change-1", "image-2"),
        )
        self.assertEqual(result["shot_nos"], ("1", "2"))

    def test_full_film_failure_returns_all_authoritative_issues(self):
        result = map_historical_review_returns(
            [{"slot_index": "full_film:匿名项目", "shot_no": "匿名项目", "status": "需修改"}],
            self.review_issues,
        )
        self.assertEqual(
            result["issue_keys"],
            ("image-1", "change-1", "image-2", "image-3"),
        )
        self.assertEqual(result["shot_nos"], ("1", "2", "3"))

    def test_unknown_failure_scope_is_blocked_instead_of_silently_approved(self):
        with self.assertRaises(WorkspaceServiceError) as raised:
            map_historical_review_returns(
                [{"slot_index": "unknown:edge", "shot_no": "edge", "status": "需修改"}],
                self.review_issues,
            )
        self.assertEqual(raised.exception.code, "REVIEW_IMPORT_MAPPING_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
