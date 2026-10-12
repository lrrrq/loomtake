import json
import unittest
from pathlib import Path

from mvp_v2.revision_service import (
    LINEAGE_RELATIONS,
    build_revision_report,
    compare_versions,
    version_shots_from_artifacts,
)


FIXTURE_PATH = Path(__file__).with_name("fixtures") / "revision_lineage_cases.json"


class RevisionServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.cases = {item["name"]: item for item in payload["cases"]}

    def _compare(self, name):
        fixture = self.cases[name]
        return compare_versions(
            project_id="project_anonymous",
            parent_version_id=f"version_{name}_parent",
            child_version_id=f"version_{name}_child",
            parent_shots=fixture["parent"],
            child_shots=fixture["child"],
        )

    def test_fixed_fixtures_cover_every_lineage_relation_and_ambiguity(self):
        covered = set()
        for name, fixture in self.cases.items():
            result = self._compare(name)
            self.assertEqual(result["summary"]["relation_counts"], fixture["expected"], name)
            covered.update(result["summary"]["relation_counts"])

        self.assertEqual(covered, set(LINEAGE_RELATIONS) | {"lineage_needs_review"})

    def test_preserved_and_moved_shots_inherit_stable_parent_identity(self):
        for name in ("preserved", "moved"):
            first = self._compare(name)
            second = self._compare(name)
            self.assertEqual(first, second)
            parent_ids = {shot["shot_id"] for shot in first["parent_shots"]}
            for shot_diff in first["shot_diffs"]:
                self.assertIn(shot_diff["child_shot_id"], parent_ids)

    def test_fixed_fixture_edges_are_exact_not_only_counted(self):
        expected = {
            "preserved": [("preserved", ("1",), "1")],
            "moved": [("moved", ("2",), "1"), ("moved", ("1",), "2")],
            "split_from": [
                ("split_from", ("1",), "1"),
                ("split_from", ("1",), "2"),
            ],
            "merged_from": [
                ("merged_from", ("1",), "1"),
                ("merged_from", ("2",), "1"),
            ],
            "new": [("preserved", ("1",), "1"), ("new", (), "2")],
            "deleted": [("preserved", ("1",), "1"), ("deleted", ("2",), "")],
        }
        for name, expected_edges in expected.items():
            result = self._compare(name)
            actual = [
                (item["relation"], tuple(item["parent_shot_nos"]), item["child_shot_no"])
                for item in result["shot_diffs"]
            ]
            self.assertEqual(actual, expected_edges, name)

    def test_field_diff_reports_changed_and_unchanged_values(self):
        result = self._compare("preserved")
        shot_diff = result["shot_diffs"][0]
        by_field = {item["field"]: item for item in shot_diff["field_diffs"]}

        self.assertEqual(by_field["copy"]["status"], "changed")
        self.assertEqual(by_field["copy"]["before"], "old")
        self.assertEqual(by_field["copy"]["after"], "new")
        self.assertEqual(by_field["structure"]["status"], "unchanged")
        self.assertEqual(result["summary"]["changed_field_count"], 1)
        self.assertEqual(result["summary"]["field_change_counts"], {"copy": 1})

    def test_current_artifacts_adapt_without_losing_source_triple_or_image_field(self):
        shots = version_shots_from_artifacts(
            [{"镜号": "1", "结构": "opening", "画面描述": "anonymous"}],
            [{
                "采用来源文件": "anonymous.xlsx",
                "采用来源 sheet": "template",
                "采用来源行": 7,
            }],
            bindings=[{"shot_no": "1", "image_id": "media_anonymous", "resolution_status": "resolved"}],
            shot_ids_by_no={"1": "shot_existing_identity"},
        )

        self.assertEqual(shots[0]["shot_id"], "shot_existing_identity")
        self.assertEqual(shots[0]["fields"]["image_id"], "media_anonymous")
        self.assertEqual(shots[0]["source_refs"][0], {
            "source_file": "anonymous.xlsx",
            "source_sheet": "template",
            "source_row": 7,
        })

    def test_ambiguous_fixture_never_silently_selects_lineage(self):
        result = self._compare("ambiguous")

        self.assertTrue(result["lineage_needs_review"])
        self.assertEqual(len(result["lineage_review_items"]), 2)
        self.assertFalse(result["shot_diffs"])
        for item in result["lineage_review_items"]:
            self.assertEqual(len(item["candidate_parent_shot_ids"]), 2)

    def test_repeated_source_can_only_be_disambiguated_by_exact_content(self):
        source = [{
            "source_file": "anonymous.xlsx",
            "source_sheet": "template",
            "source_row": 7,
        }]
        result = compare_versions(
            project_id="project_anonymous",
            parent_version_id="version_content_parent",
            child_version_id="version_content_child",
            parent_shots=[
                {"shot_no": "1", "fields": {"copy": "A"}, "source_refs": source},
                {"shot_no": "2", "fields": {"copy": "B"}, "source_refs": source},
            ],
            child_shots=[
                {"shot_no": "1", "fields": {"copy": "B"}, "source_refs": source},
                {"shot_no": "2", "fields": {"copy": "A"}, "source_refs": source},
            ],
        )

        self.assertFalse(result["lineage_needs_review"])
        self.assertEqual(result["summary"]["relation_counts"], {"moved": 2})

    def test_explicit_new_shot_identity_is_not_overwritten_by_source_similarity(self):
        fixture = self.cases["preserved"]
        child = json.loads(json.dumps(fixture["child"]))
        child[0]["shot_id"] = "shot_explicitly_new"
        result = compare_versions(
            project_id="project_anonymous",
            parent_version_id="version_explicit_parent",
            child_version_id="version_explicit_child",
            parent_shots=fixture["parent"],
            child_shots=child,
        )

        self.assertEqual(result["summary"]["relation_counts"], {"new": 1, "deleted": 1})
        self.assertEqual(result["child_shots"][0]["shot_id"], "shot_explicitly_new")

    def test_revision_report_verifies_locks_and_preserves_review_semantics(self):
        diff = self._compare("preserved")
        report = build_revision_report(
            diff=diff,
            original_feedback="只修改文案，结构不要动",
            structured_intent={
                "scope": "shot",
                "shot_no": "1",
                "requested_fields": ["copy"],
            },
            expected_impact={
                "shot_nos": ["1"],
                "fields": ["copy"],
                "earliest_restart_stage": "script_draft",
                "confidence": "high",
            },
            locked_fields=[{"shot_no": "1", "field": "structure"}],
            rerun_nodes=["script_draft", "visual_binding", "workbook_render"],
            unresolved_items=[],
            machine_review={"status": "pass"},
            human_review={"status": "waiting_human_review"},
            publication={"status": "unpublished"},
        )

        self.assertEqual(report["completion"]["status"], "waiting_human_review")
        self.assertFalse(report["locked_field_verification"]["violations"])
        self.assertEqual(report["actual_impact"]["changed_fields"], ["copy"])
        self.assertEqual(report["actual_impact"]["changed_shot_nos"], ["1"])
        self.assertEqual(report["rerun_nodes"][0], "script_draft")
        self.assertEqual(report["statuses"]["human_review"]["status"], "waiting_human_review")
        self.assertEqual(report["statuses"]["publication"]["status"], "unpublished")

    def test_revision_report_blocks_lock_violation_and_lineage_ambiguity(self):
        violated = build_revision_report(
            diff=self._compare("preserved"),
            original_feedback="文案不要动",
            structured_intent={},
            expected_impact={},
            locked_fields=[{"shot_no": "1", "field": "copy"}],
            rerun_nodes=[],
            unresolved_items=[],
            machine_review={"status": "pass"},
            human_review={"status": "waiting_human_review"},
            publication={"status": "unpublished"},
        )
        ambiguous = build_revision_report(
            diff=self._compare("ambiguous"),
            original_feedback="调整结构",
            structured_intent={},
            expected_impact={},
            locked_fields=[],
            rerun_nodes=["script_plan"],
            unresolved_items=[],
            machine_review={"status": "pass"},
            human_review={"status": "waiting_human_review"},
            publication={"status": "unpublished"},
        )

        self.assertEqual(violated["completion"]["status"], "blocked")
        self.assertEqual(len(violated["locked_field_verification"]["violations"]), 1)
        self.assertEqual(ambiguous["completion"]["status"], "lineage_needs_review")

    def test_revision_report_blocks_when_locked_target_cannot_be_verified(self):
        report = build_revision_report(
            diff=self._compare("preserved"),
            original_feedback="镜 99 文案不能动",
            structured_intent={},
            expected_impact={"fields": ["copy"], "shot_nos": ["1"]},
            locked_fields=[{"shot_no": "99", "field": "copy"}],
            rerun_nodes=["script_draft"],
            unresolved_items=[],
            machine_review={"status": "pass"},
            human_review={"status": "quality_pass"},
            publication={"status": "unpublished"},
        )

        self.assertEqual(report["completion"]["reason"], "locked_field_violation")
        self.assertEqual(report["locked_field_verification"]["violations"][0]["status"], "not_found")

    def test_revision_report_rejects_published_state_without_human_quality_pass(self):
        report = build_revision_report(
            diff=self._compare("preserved"),
            original_feedback="修改文案",
            structured_intent={},
            expected_impact={},
            locked_fields=[],
            rerun_nodes=["script_draft"],
            unresolved_items=[],
            machine_review={"status": "pass"},
            human_review={"status": "waiting_human_review"},
            publication={"status": "published"},
        )

        self.assertEqual(report["completion"], {
            "status": "blocked",
            "completed": False,
            "reason": "publication_gate_violation",
        })

    def test_revision_report_cannot_complete_when_requested_impact_is_missing(self):
        fixture = self.cases["preserved"]
        unchanged_child = json.loads(json.dumps(fixture["parent"]))
        diff = compare_versions(
            project_id="project_anonymous",
            parent_version_id="version_unchanged_parent",
            child_version_id="version_unchanged_child",
            parent_shots=fixture["parent"],
            child_shots=unchanged_child,
        )
        report = build_revision_report(
            diff=diff,
            original_feedback="修改镜 1 文案",
            structured_intent={"requested_fields": ["copy"]},
            expected_impact={"fields": ["copy"], "shot_nos": ["1"]},
            locked_fields=[],
            rerun_nodes=["script_draft"],
            unresolved_items=[],
            machine_review={"status": "pass"},
            human_review={"status": "quality_pass"},
            publication={"status": "unpublished"},
        )

        self.assertEqual(report["completion"]["status"], "partially_completed")
        self.assertEqual(report["completion"]["reason"], "expected_impact_missing")
        self.assertEqual(report["completion"]["missing"], {
            "fields": ["copy"],
            "shot_nos": ["1"],
        })

    def test_revision_report_cannot_complete_with_unexpected_impact(self):
        report = build_revision_report(
            diff=self._compare("preserved"),
            original_feedback="只改结构",
            structured_intent={"requested_fields": ["structure"]},
            expected_impact={"fields": ["structure"], "shot_nos": ["1"]},
            locked_fields=[],
            rerun_nodes=["script_plan"],
            unresolved_items=[],
            machine_review={"status": "pass"},
            human_review={"status": "quality_pass"},
            publication={"status": "unpublished"},
        )

        self.assertEqual(report["completion"]["status"], "partially_completed")
        self.assertEqual(report["completion"]["reason"], "impact_mismatch")
        self.assertEqual(report["completion"]["unexpected"]["fields"], ["copy"])
        self.assertEqual(report["completion"]["missing"]["fields"], ["structure"])

    def test_revision_report_requires_explicit_machine_pass(self):
        common = {
            "diff": self._compare("preserved"),
            "original_feedback": "修改文案",
            "structured_intent": {"requested_fields": ["copy"]},
            "expected_impact": {"fields": ["copy"], "shot_nos": ["1"]},
            "locked_fields": [],
            "rerun_nodes": ["script_draft"],
            "unresolved_items": [],
            "human_review": {"status": "quality_pass"},
            "publication": {"status": "unpublished"},
        }
        pending = build_revision_report(
            **common,
            machine_review={"status": "pending"},
        )
        unknown = build_revision_report(
            **common,
            machine_review={"status": "generated"},
        )

        self.assertEqual(pending["completion"], {
            "status": "waiting_machine_review",
            "completed": False,
        })
        self.assertEqual(unknown["completion"], {
            "status": "blocked",
            "completed": False,
            "reason": "machine_review_status_unknown",
        })


if __name__ == "__main__":
    unittest.main()
