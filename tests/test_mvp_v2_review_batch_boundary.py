from __future__ import annotations

import hashlib
import unittest

from mvp_v2.manual_review_commands import _issue_key
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_review_workspace as review_fixture


class ReviewBatchBoundaryTests(unittest.TestCase):
    """Mixed review batches must preserve atomicity and exact human scope."""

    def setUp(self):
        fixture = review_fixture.FormalReviewWorkspaceTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.review = fixture.review
        self.store = fixture.store
        self.root = fixture.root
        self.token = fixture.token
        self.scope = {"workspace_id": fixture.workspace_id, "project_id": fixture.project_id}

    def _state(self):
        with self.store.connection() as connection:
            tables = [str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ) if str(row[0]).startswith(("version_review_", "version_quality_", "manual_command_"))
                or str(row[0]) in {"projects", "project_versions", "version_reviews"}]
            rows = {name: [tuple(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY 1')]
                    for name in tables}
        artifacts = {}
        for folder in ("review-evidence", "quality-evidence"):
            for path in sorted((self.root / folder).rglob("*")):
                if path.is_file():
                    artifacts[str(path.relative_to(self.root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return rows, artifacts

    def _start(self, operation):
        current = self.review.read_current(self.token, **self.scope)
        return self.review.start_review(
            self.token, **self.scope, expected_project_version=current["project_version"],
            idempotency_key=operation,
        )

    def test_two_current_items_plus_old_or_preview_item_write_no_decision_or_artifact(self):
        preview = self.review.read_current(self.token, **self.scope)
        preview_key = preview["issues"][0]["issue_key"]
        self.assertEqual(preview_key, "", "只读待办没有可提交的审核键")
        parent = self._start("batch-boundary-parent")
        old_key = parent["issues"][0]["issue_key"]

        script = self.fixture.base.base.workspace
        current = script.read_current(self.token, **self.scope)
        target = next(shot for shot in current["shots"] if shot["shot_no"] == "3")
        changed = script.apply_table_change(
            self.token, **self.scope, expected_project_version=current["project_version"],
            expected_revision=current["revision"], shot_no="3", shot_id=target["shot_id"],
            field="sound", value="持续底乐延续，按第3镜动作平稳衔接前后现场声。",
            idempotency_key="batch-boundary-sound-edit",
        )
        script.save_version(
            self.token, **self.scope, expected_project_version=current["project_version"],
            expected_revision=changed["revision"], reason="仅更新第3镜声音",
            idempotency_key="batch-boundary-save-child",
        )
        child = self._start("batch-boundary-child")
        self.assertNotEqual(child["base_version_id"], parent["base_version_id"])
        current_keys = tuple(item["issue_key"] for item in child["issues"] if item["status"] == "open")
        self.assertGreaterEqual(len(current_keys), 3)
        self.assertNotIn(old_key, current_keys)
        for label, invalid_key in (("preview", preview_key), ("old-version", old_key)):
            for decision in ("approve", "return"):
                with self.subTest(invalid=label, decision=decision):
                    before = self._state()
                    with self.assertRaises(WorkspaceServiceError):
                        self.review.decide(
                            self.token, **self.scope, expected_project_version=child["project_version"],
                            issue_keys=(*current_keys[:2], invalid_key), decision=decision,
                            note="共同说明只适用于已明确选择的当前版本审核项。",
                            idempotency_key=f"batch-boundary-{label}-{decision}",
                        )
                    self.assertEqual(self._state(), before,
                                     "无效混合批次不得新增人工决定、命令记录或审核证据文件")

    def test_one_common_return_reason_is_bound_to_each_selected_item_and_no_others(self):
        started = self._start("batch-common-start")
        selected = tuple(item["issue_key"] for item in started["issues"][:2])
        all_keys = {item["issue_key"] for item in started["issues"]}
        self.assertGreater(len(all_keys), len(selected))
        before_rows, before_files = self._state()
        note = "所选镜头的画面重点与图片内容需重新核对，修改后再提交审核。"
        returned = self.review.decide(
            self.token, **self.scope, expected_project_version=started["project_version"],
            issue_keys=selected, decision="return", note=note,
            idempotency_key="batch-common-return",
        )
        after_rows, after_files = self._state()
        self.assertEqual(returned["state"], "needs_change")
        returned_issues = {item["issue_key"]: item for item in returned["issues"]}
        for key in all_keys:
            self.assertEqual(returned_issues[key]["status"], "return" if key in selected else "open")

        with self.store.connection() as connection:
            decisions = [dict(row) for row in connection.execute(
                "SELECT * FROM version_review_human_decisions WHERE version_id=? ORDER BY decision_id",
                (started["base_version_id"],),
            )]
        self.assertEqual(len(decisions), 2)
        self.assertEqual({_issue_key(row["review_item_id"]) for row in decisions}, set(selected))
        new_files = set(after_files) - set(before_files)
        self.assertEqual(len(new_files), 2, "只为两个明确选择的审核项记录人工证据")
        for row in decisions:
            payload = self.review.services.commands.services.evidence_authority.artifacts.read_json(
                row["evidence_ref"], row["evidence_sha256"],
            )
            self.assertEqual(payload["kind"], "human-decision")
            self.assertEqual(payload["decision"], "return")
            self.assertEqual(payload["note"], note)
            self.assertEqual(payload["review_item_id"], row["review_item_id"])
            self.assertEqual(payload["version_id"], started["base_version_id"])
            self.assertEqual(payload["project_id"], self.scope["project_id"])
            self.assertEqual(payload["actor_id"], row["reviewer_actor_id"])
            self.assertEqual(payload["occurred_at"], row["decided_at"])
            self.assertTrue(any(path.endswith(f'{row["evidence_sha256"]}.json') for path in new_files))
        for path, digest in before_files.items():
            self.assertEqual(after_files[path], digest, "既有人工审核证据不可改写")
        for table in ("version_review_items", "version_quality_final_confirmations", "project_versions"):
            self.assertEqual(after_rows[table], before_rows[table])


if __name__ == "__main__":
    unittest.main()
