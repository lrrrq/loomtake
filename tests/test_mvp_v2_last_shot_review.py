"""Deleting the closing shot must reopen the new closing shot's review."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from openpyxl import load_workbook
from pypdf import PdfReader

from mvp_v2.formal_review_workspace import FormalReviewWorkspaceService
from mvp_v2.revision_service import compare_versions
from tests import test_mvp_v2_formal_review_workspace as review_fixture


class LastShotReviewTests(unittest.TestCase):
    def test_deleting_last_shot_reopens_new_last_shot_and_its_adjacent_edge(self):
        fixture = review_fixture.FormalReviewWorkspaceTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        parent = fixture._confirm_current()
        script = fixture.base.base.workspace
        scope = {"workspace_id": fixture.workspace_id, "project_id": fixture.project_id}
        current = script.read_current(fixture.token, **scope)
        self.assertGreaterEqual(len(current["shots"]), 8)
        versions_before = self._versions(fixture)
        parent_version = next(row for row in versions_before if row["version_id"] == parent["base_version_id"])
        parent_paths = [fixture.base.resolver.resolve_stage_artifact(parent_version["run_id"], name).path
                        for name in ("script_plan", "script_draft", "visual_binding")]
        parent_paths.append(fixture.base.resolver.resolve_artifact(parent_version["run_id"], "workbook").path)
        parent_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in parent_paths}
        parent_decisions = self._image_decisions(fixture, parent["base_version_id"])
        parent_approvals = self._image_approvals(fixture, parent["base_version_id"])
        self.assertEqual(len(parent_decisions), len(current["shots"]))
        self.assertEqual(len(parent_approvals), len(current["shots"]))
        last = current["shots"][-1]
        new_last = current["shots"][-2]
        changed = script.apply_revision_operation(
            fixture.token, **scope,
            expected_project_version=current["project_version"], expected_revision=current["revision"],
            operation={"operation": "delete_shot", "shot_no": last["shot_no"], "shot_id": last["shot_id"]},
            idempotency_key="last-shot-review-delete",
        )
        saved = script.save_version(
            fixture.token, **scope,
            expected_project_version=current["project_version"], expected_revision=changed["revision"],
            reason="删除末镜后重新检查收束", idempotency_key="last-shot-review-save",
        )
        self.assertEqual(saved["save_status"], "completed")
        preview = fixture.review.read_current(fixture.token, **scope)
        self.assertNotEqual(preview["base_version_id"], parent["base_version_id"])
        versions_after = self._versions(fixture)
        self.assertEqual(len(versions_after), len(versions_before) + 1)
        children = [row for row in versions_after if row["parent_version_id"] == parent["base_version_id"]]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["version_id"], preview["base_version_id"])
        expected_decisions = {number: value for number, value in parent_decisions.items() if number != last["shot_no"]}
        self.assertEqual(self._image_decisions(fixture, preview["base_version_id"]), expected_decisions)
        images = fixture.base.workspace.read_current(fixture.token, **scope)
        self.assertEqual(images["state"], "confirmed")
        self.assertTrue(images["review_eligible"])
        self.assertTrue(images["delivery_eligible"])
        self.assertEqual(len(images["shots"]), len(current["shots"]) - 1)
        self.assertTrue(all(item["confirmed"] for item in images["shots"]))
        self.assertNotEqual(preview["state"], "blocked", preview)
        self.assertIsNone(preview["final_confirmation"])
        focused = [item for item in preview["issues"] if item["kind"] == "change"]
        last_changes = [item for item in focused if item["shot_no"] == new_last["shot_no"]]
        self.assertEqual(len(last_changes), 1, [{key: item[key] for key in ("kind", "shot_no", "status", "change_summary")} for item in preview["issues"]])
        self.assertTrue(last_changes[0]["focus_required"])
        self.assertEqual(last_changes[0]["affected_adjacent_edges"], [f"{int(new_last['shot_no']) - 1}->{new_last['shot_no']}"])
        self.assertTrue(last_changes[0]["change_summary"].strip())
        started = fixture.review.start_review(
            fixture.token, **scope, expected_project_version=preview["project_version"],
            idempotency_key="last-shot-review-start-child",
        )
        self.assertEqual(self._image_approvals(fixture, started["base_version_id"]), {
            number: value for number, value in parent_approvals.items() if number != last["shot_no"]
        })
        pending = tuple(item["issue_key"] for item in started["issues"] if item["status"] in {"open", "recheck"})
        self.assertTrue(pending)
        approved = fixture.review.decide(
            fixture.token, **scope, expected_project_version=started["project_version"],
            issue_keys=pending, decision="approve", note="已核对删除末镜后的新收束镜和相邻声音承接",
            idempotency_key="last-shot-review-approve-child",
        )
        final = fixture.review.confirm_final(
            fixture.token, **scope, expected_project_version=approved["project_version"],
            note="已检查删除末镜后的完整版本", idempotency_key="last-shot-review-final-child",
        )
        self.assertEqual(final["state"], "confirmed")
        excel = fixture.review.prepare_excel_delivery(
            fixture.token, **scope, expected_project_version=final["project_version"],
        )
        pdf = fixture.review.prepare_pdf_delivery(
            fixture.token, **scope, expected_project_version=final["project_version"],
        )
        self.assertEqual(pdf.status, "available", pdf.unavailable_reason)
        expected_count = len(current["shots"]) - 1
        workbook = load_workbook(excel.path)
        try:
            self.assertEqual(len(workbook["脚本"]._images), expected_count)
            self.assertEqual(workbook["相邻镜头复核"].max_row - 1, expected_count - 1)
        finally:
            workbook.close()
        self.assertEqual(len(PdfReader(str(pdf.path)).pages), expected_count)
        self.assertEqual(len(self._versions(fixture)), len(versions_before) + 1)
        for path, digest in parent_hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, str(path))
        evidence_parent = os.environ.get("MPLUS_COMBINED_RETURN_EVIDENCE_ROOT", "").strip()
        if evidence_parent:
            Path(evidence_parent).mkdir(parents=True, exist_ok=True)
            output = Path(tempfile.mkdtemp(prefix="new-last-shot-", dir=evidence_parent))
            shutil.copy2(excel.path, output / "new-last-shot.xlsx")
            shutil.copy2(pdf.path, output / "new-last-shot.pdf")
            (output / "result.json").write_text(json.dumps({
                "status": "passed", "parent_version_id": parent["base_version_id"],
                "child_version_id": final["base_version_id"], "shot_count": expected_count,
                "new_last_shot": new_last["shot_no"], "focused_review": last_changes,
                "new_child_count": 1, "inherited_image_decisions": len(expected_decisions),
                "parent_sha256": {path.name: digest for path, digest in parent_hashes.items()},
                "excel_sha256": hashlib.sha256(excel.path.read_bytes()).hexdigest(),
                "pdf_sha256": hashlib.sha256(pdf.path.read_bytes()).hexdigest(),
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _versions(fixture):
        with fixture.store.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM project_versions WHERE project_id=?", (fixture.project_id,),
            )]

    @staticmethod
    def _image_decisions(fixture, version_id):
        with fixture.store.connection() as connection:
            rows = connection.execute("""SELECT d.shot_no, d.project_image_id, d.image_sha256,
                d.analysis_version_id, d.source_kind, d.source_key, d.source_location_json,
                d.decided_by_actor_id, d.decided_at
                FROM version_image_decisions_v3 d JOIN version_image_decision_snapshots_v3 s
                ON s.snapshot_id=d.snapshot_id WHERE s.materialized_version_id=?""", (version_id,)).fetchall()
        return {str(row["shot_no"]): dict(row) for row in rows}

    @staticmethod
    def _image_approvals(fixture, version_id):
        with fixture.store.connection() as connection:
            rows = connection.execute("""SELECT i.shot_no, d.reviewer_actor_id, d.decided_at, d.decision
                FROM version_review_items i JOIN version_review_human_decisions d
                ON d.review_item_id=i.review_item_id WHERE i.version_id=? AND i.kind='image'""", (version_id,)).fetchall()
        return {str(row["shot_no"]): dict(row) for row in rows}

    def test_first_middle_deletions_focus_new_boundaries_from_canonical_diff(self):
        parents = [{"shot_no": str(index), "shot_id": f"shot-{index}", "fields": {"画面描述": f"匿名镜头{index}"},
                    "source_refs": [{"source_file": "anonymous.xlsx", "source_sheet": "template", "source_row": index + 6}]}
                   for index in range(1, 7)]
        for deleted, expected in (("1", {"1"}), ("3", {"2", "3"})):
            with self.subTest(deleted=deleted), tempfile.TemporaryDirectory() as temporary:
                children = [{**shot, "shot_no": str(index)} for index, shot in enumerate(
                    [shot for shot in parents if shot["shot_no"] != deleted], start=1)]
                diff = compare_versions(project_id="project", parent_version_id="parent", child_version_id="child",
                                        parent_shots=parents, child_shots=children)
                path = Path(temporary) / "revision_report.json"
                path.write_text(json.dumps({"child_version_id": "child", "version_diff": diff}), encoding="utf-8")
                artifacts = Mock()
                artifacts.resolve_artifact.return_value = SimpleNamespace(path=path)
                service = object.__new__(FormalReviewWorkspaceService)
                service.services = SimpleNamespace(artifacts=artifacts)
                actual = service._changed_shots(SimpleNamespace(run_id="run", version_id="child"))
                self.assertEqual({number for number, fields in actual.items() if "镜头承接" in fields}, expected)
                self.assertEqual({number for number, fields in actual.items() if "首镜开场" in fields}, {"1"} if deleted == "1" else set())


if __name__ == "__main__":
    unittest.main()
