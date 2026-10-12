import unittest

from mvp_v2.local_ui import (
    _run_historical_review_import_saga,
    map_historical_review_returns,
)
from mvp_v2.workspace_store import WorkspaceServiceError


def _review_model(statuses=None, *, confirmed=False):
    statuses = statuses or {}
    issues = [
        {
            "issue_key": f"review-image-{shot}",
            "kind": "image",
            "shot_no": str(shot),
            "status": statuses.get(f"review-image-{shot}", "open"),
        }
        for shot in range(1, 6)
    ] + [
        {
            "issue_key": f"review-change-{shot}",
            "kind": "change",
            "shot_no": str(shot),
            "status": statuses.get(f"review-change-{shot}", "open"),
        }
        for shot in range(1, 6)
    ]
    unresolved = [item for item in issues if item["status"] != "approve"]
    return {
        "project_version": 1,
        "issues": issues,
        "state": "confirmed" if confirmed else (
            "needs_change" if any(item["status"] == "return" for item in issues)
            else "ready_final" if not unresolved else "reviewing"
        ),
        "ready_for_final": not unresolved and not confirmed,
    }


class FakeTasks:
    def __init__(self):
        self.task = {"task_id": "proposal-1", "status": "proposed", "task_version": 0}
        self.events = []

    def get_task(self, _auth_token, _task_id):
        return dict(self.task)

    def transition_task(self, _auth_token, *, task_id, expected_task_version, status, details=None, **kwargs):
        self.assert_task(task_id, expected_task_version)
        self.task["status"] = status
        self.task["task_version"] += 1
        self.events.append({"status": status, "details": dict(details or {}), **kwargs})
        return dict(self.task)

    def assert_task(self, task_id, expected_task_version):
        if task_id != self.task["task_id"] or expected_task_version != self.task["task_version"]:
            raise AssertionError("task version mismatch")


class FakeReview:
    def __init__(self, *, failure_step=None, fail_after_write=False):
        self.statuses = {}
        self.final_confirmed = False
        self.failure_step = failure_step
        self.fail_after_write = fail_after_write
        self.calls = []
        self.decisions = []
        self.final_calls = 0

    def _fail(self, step):
        if self.failure_step != step:
            return
        if not self.fail_after_write:
            self.failure_step = None
            raise RuntimeError(f"injected {step} failure")

    def start_review(self, _auth_token, **_kwargs):
        self.calls.append(("start", ()))
        self._fail("start")
        return _review_model(self.statuses, confirmed=self.final_confirmed)

    def decide(self, _auth_token, *, issue_keys, decision, **_kwargs):
        self.calls.append((decision, tuple(issue_keys)))
        self._fail(decision)
        for index, key in enumerate(issue_keys):
            self.statuses[key] = decision
            self.decisions.append((key, decision))
            if self.failure_step == decision and self.fail_after_write and index == 0:
                self.failure_step = None
                self.fail_after_write = False
                raise RuntimeError(f"injected {decision} failure after write")
        return _review_model(self.statuses, confirmed=self.final_confirmed)

    def confirm_final(self, _auth_token, **_kwargs):
        self.calls.append(("final", ()))
        self.final_calls += 1
        self._fail("final")
        self.final_confirmed = True
        if self.fail_after_write:
            self.fail_after_write = False
            raise RuntimeError("injected final failure after write")
        return _review_model(self.statuses, confirmed=True)


def _report(issues, human_review_status="quality_pass"):
    return {
        "projects": [{
            "project_name": "匿名5镜",
            "human_review_status": human_review_status,
            "total_shot_count": 5,
            "issues": issues,
        }],
        "adjacent_edges": [f"{index}->{index + 1}" for index in range(1, 5)],
    }


class HistoricalReviewScopeTests(unittest.TestCase):
    def test_anonymous_five_shot_adjacent_full_film_and_mixed_scopes_map_exactly(self):
        review = _review_model()
        mapped = map_historical_review_returns(_report([
            {"slot_index": "2", "shot_no": "2"},
            {"slot_index": "adjacent:3->4", "shot_no": "3->4"},
        ])["projects"][0]["issues"], review["issues"])
        self.assertEqual(mapped["shot_nos"], ("2", "3", "4"))
        self.assertEqual(
            set(mapped["issue_keys"]),
            {
                "review-image-2", "review-change-2",
                "review-image-3", "review-change-3",
                "review-image-4", "review-change-4",
            },
        )

        full = map_historical_review_returns(_report([
            {"slot_index": "full_film:匿名5镜", "shot_no": "匿名5镜"},
        ])["projects"][0]["issues"], review["issues"])
        self.assertEqual(full["shot_nos"], ("1", "2", "3", "4", "5"))
        self.assertEqual(len(full["issue_keys"]), 10)
        self.assertEqual(_report([])["adjacent_edges"], ["1->2", "2->3", "3->4", "4->5"])

    def test_unknown_scope_fails_closed(self):
        with self.assertRaises(WorkspaceServiceError) as raised:
            map_historical_review_returns(_report([
                {"slot_index": "adjacent:2->9", "shot_no": "2->9"},
            ])["projects"][0]["issues"], _review_model()["issues"])
        self.assertEqual(raised.exception.code, "REVIEW_IMPORT_MAPPING_UNSUPPORTED")


class HistoricalReviewSagaTests(unittest.TestCase):
    def _run(self, review, report):
        tasks = FakeTasks()
        with self.assertRaises((RuntimeError, WorkspaceServiceError)):
            _run_historical_review_import_saga(
                tasks=tasks,
                formal_review_service=review,
                auth_token="auth",
                task=tasks.get_task("auth", "proposal-1"),
                proposal_id="proposal-1",
                proposal_digest="d" * 64,
                workspace_id="workspace-1",
                project_id="project-1",
                expected_project_version=1,
                report=report,
            )
        return tasks

    def test_return_precedes_approve_and_return_is_never_overwritten(self):
        review = FakeReview()
        tasks = FakeTasks()
        result = _run_historical_review_import_saga(
            tasks=tasks, formal_review_service=review, auth_token="auth",
            task=tasks.get_task("auth", "proposal-1"), proposal_id="proposal-1",
            proposal_digest="d" * 64, workspace_id="workspace-1", project_id="project-1",
            expected_project_version=1,
            report=_report([{"slot_index": "2", "shot_no": "2"}], "quality_fail"),
        )
        self.assertEqual(tasks.task["status"], "completed")
        self.assertEqual(result["review_state"], "needs_change")
        self.assertEqual([call[0] for call in review.calls], ["start", "return", "approve"])
        self.assertNotIn("review-image-2", review.calls[-1][1])
        self.assertNotIn("review-change-2", review.calls[-1][1])
        self.assertEqual(len(review.decisions), 10)

    def test_start_return_approve_and_final_failures_block_then_resume_without_duplicate_writes(self):
        cases = (
            ("start", _report([]), False),
            ("return", _report([{"slot_index": "2", "shot_no": "2"}], "quality_fail"), True),
            ("approve", _report([]), True),
            ("final", _report([]), True),
        )
        for failure_step, report, fail_after_write in cases:
            with self.subTest(failure_step=failure_step):
                review = FakeReview(failure_step=failure_step, fail_after_write=fail_after_write)
                tasks = self._run(review, report)
                self.assertEqual(tasks.task["status"], "blocked")
                retry = _run_historical_review_import_saga(
                    tasks=tasks, formal_review_service=review, auth_token="auth",
                    task=tasks.get_task("auth", "proposal-1"), proposal_id="proposal-1",
                    proposal_digest="d" * 64, workspace_id="workspace-1", project_id="project-1",
                    expected_project_version=1, report=report,
                )
                self.assertEqual(tasks.task["status"], "completed")
                self.assertEqual(retry["status"], "completed")
                if failure_step == "final":
                    self.assertEqual(review.final_calls, 1)
                else:
                    self.assertEqual(len(review.decisions), 10)

    def test_unknown_scope_blocks_before_any_decision_or_final(self):
        review = FakeReview()
        tasks = self._run(review, _report([
            {"slot_index": "unknown:2->3", "shot_no": "2->3"},
        ]))
        self.assertEqual(tasks.task["status"], "blocked")
        self.assertEqual([call[0] for call in review.calls], ["start"])


if __name__ == "__main__":
    unittest.main()
