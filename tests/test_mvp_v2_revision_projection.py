from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from openpyxl import load_workbook

from mvp_v2.formal_script_workspace import DraftVersionMaterializer
from mvp_v2.excel_io import ADJACENT_REVIEW_SHEET_NAME, read_managed_sheet_rows
from mvp_v2 import formal_script_workspace as workspace_module
from mvp_v2.run_contract import sha256_file
from mvp_v2.workspace_store import WorkspaceServiceError
from mvp_v2.version_review_service import Draft
from tests import test_mvp_v2_formal_revision_union as union_fixture


class FrozenRevisionProjectionTests(unittest.TestCase):
    """Independent three-source oracle for a saved plan's nested identities."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-projection-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.parent = self.root / "parent-plan.json"
        self.payload = {
            "shot_count": 3,
            "decisions": [
                {"slot": {"shot_no": str(i), "slot_index": i, "source_row": 20 + i},
                 "variant": {"shot_no": str(40 + i), "source_file": "匿名.xlsx",
                             "source_sheet": "来源", "source_row": 80 + i}, "mode": "ADAPT"}
                for i in range(1, 4)
            ],
            "template_slots": [
                {"shot_no": str(i), "slot_index": i, "source_row": 20 + i}
                for i in range(1, 4)
            ],
            "shot_plans": [
                {"shot_no": str(i), "event_ids": [f"event-{i}"],
                 "field_decisions": [{"field": "声音", "shot_no": str(i)}]}
                for i in range(1, 4)
            ],
            "field_decisions": [{"shot_no": str(i), "decisions": []} for i in range(1, 4)],
            "coverage": [{"requirement_id": "requirement-3", "shot_nos": ["2", "3"], "status": "covered"}],
            "traceability": [{
                "requirement_id": "requirement-3", "plan_shot_nos": ["2", "3"],
                "output_evidence": [{"shot_no": "2", "event_ids": ["event-2"]},
                                    {"shot_no": "3", "event_ids": ["event-3"]}],
                "final_status": "covered",
            }],
        }
        self.parent.write_text(json.dumps(self.payload), encoding="utf-8")

    def _project(self, origins):
        shots = tuple({"origin_shot_no": str(origin), "shot_no": str(i)}
                      for i, origin in enumerate(origins, 1))
        draft = Draft("draft", "workspace", "project", "parent", 1, shots,
                      "open", "actor", "2026-09-09T00:00:00Z")
        details = [{"模板镜号": str(i), "采用来源文件": "匿名.xlsx",
                    "采用来源 sheet": "来源", "采用来源行": 80 + origin}
                   for i, origin in enumerate(origins, 1)]
        output = self.root / "child-plan.json"
        before = self.parent.read_bytes()
        with patch("mvp_v2.formal_script_workspace.start_pipeline_stage"), \
             patch("mvp_v2.formal_script_workspace.finish_pipeline_stage"):
            DraftVersionMaterializer._write_plan_stage(
                SimpleNamespace(run_dir=self.root), parent=self.parent, output=output,
                script_rows=[{"镜号": str(i), "结构": "活动"} for i in range(1, len(origins) + 1)],
                detail_rows=details, draft=draft, request_sha256="d" * 64,
            )
        self.assertEqual(self.parent.read_bytes(), before)
        return json.loads(output.read_text())

    def test_nested_decisions_keep_source_identity_for_sound_delete_and_reorder(self):
        for origins in ((1, 2, 3), (2, 3), (1, 3), (1, 2), (3, 1, 2)):
            with self.subTest(origins=origins):
                child = self._project(origins)
                expected = []
                for number, origin in enumerate(origins, 1):
                    item = deepcopy(self.payload["decisions"][origin - 1])
                    item["slot"]["shot_no"] = str(number)
                    item["slot"]["slot_index"] = number
                    expected.append(item)
                self.assertEqual(child["decisions"], expected)
                self.assertEqual([x["shot_no"] for x in child["template_slots"]],
                                 [str(i) for i in range(1, len(origins) + 1)])

    def test_collection_coverage_and_traceability_keep_requirements_and_project_shots(self):
        child = self._project((2, 3))
        self.assertEqual(child["coverage"][0]["requirement_id"], "requirement-3")
        self.assertEqual(child["coverage"][0]["shot_nos"], ["1", "2"])
        self.assertEqual(len(child["traceability"]), 1)
        trace = child["traceability"][0]
        self.assertEqual(trace["plan_shot_nos"], ["1", "2"])
        self.assertEqual([x["shot_no"] for x in trace["output_evidence"]], ["1", "2"])
        self.assertEqual([x["event_ids"] for x in trace["output_evidence"]], [["event-2"], ["event-3"]])

    def test_missing_plan_recovery_rejects_disagreeing_frozen_sources(self):
        draft_path, binding_path = self.root / "draft.json", self.root / "binding.json"
        payload = deepcopy(self.payload)
        payload["script_rows"] = [{"镜号": str(i)} for i in range(1, 4)]
        payload["detail_rows"] = [{"模板镜号": str(i), "采用来源文件": "匿名.xlsx",
                                   "采用来源 sheet": "来源", "采用来源行": 80 + i}
                                  for i in range(1, 4)]
        payload["script_plan_ref"] = str(self.parent.resolve())
        draft_path.write_text(json.dumps(payload))
        bound = deepcopy(payload)
        bound["script_draft_ref"] = str(draft_path.resolve())
        bound["decisions"][1]["variant"]["source_row"] = 999
        binding_path.write_text(json.dumps(bound))
        with self.assertRaises(WorkspaceServiceError) as raised:
            workspace_module._recover_frozen_decisions(
                self.parent, {**self.payload, "decisions": []}, draft_path, binding_path,
            )
        self.assertEqual(raised.exception.code, "DRAFT_SOURCE_DRIFT")


class SavedRevisionProjectionJourneyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = union_fixture.FormalRevisionUnionTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.workspace = self.fixture.base.workspace
        self.scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.fixture.project_id}

    def _payloads(self):
        _, context = self.workspace._current_context(self.fixture.token, **self.scope)
        resolver = self.fixture.base.resolver
        paths = {stage: resolver.resolve_stage_artifact(
            context.run_id, stage, expected_manifest_sha256=context.manifest.sha256,
        ).path for stage in ("script_plan", "script_draft", "visual_binding")}
        return paths, {key: json.loads(path.read_text()) for key, path in paths.items()}

    def test_sound_save_keeps_all_three_frozen_decision_sets_and_one_child(self):
        before_paths, before = self._payloads()
        hashes = {path: sha256_file(path) for path in before_paths.values()}
        current = self.workspace.read_current(self.fixture.token, **self.scope)
        count = len(current["shots"])
        self.assertGreaterEqual(count, 3)
        changed = self.workspace.apply_table_change(
            self.fixture.token, **self.scope, expected_project_version=current["project_version"],
            expected_revision=current["revision"], shot_no="3", shot_id=current["shots"][2]["shot_id"],
            field="sound", value="掌声渐弱，保留轻音乐", idempotency_key="projection-sound",
        )
        proposal = self.fixture.revision.propose_draft_save(
            self.fixture.token, project_id=self.fixture.project_id,
            expected_project_version=current["project_version"], expected_revision=changed["revision"],
            reason="只修改第三镜声音", idempotency_key="projection-save",
        )
        confirm = {"proposal_digest": proposal["response"]["proposal_digest"]}
        first = self.fixture.agent.confirm_action(self.fixture.token, proposal["task_id"], **confirm)
        self.assertEqual(first["task"]["status"], "completed")
        self.fixture.agent.confirm_action(self.fixture.token, proposal["task_id"], **confirm)
        _, after = self._payloads()
        for key in before:
            self.assertEqual(len(after[key]["decisions"]), count, key)
            self.assertEqual(after[key]["decisions"], before[key]["decisions"], key)
            self.assertEqual(after[key]["template_slots"], before[key]["template_slots"], key)
        for path, digest in hashes.items():
            self.assertEqual(sha256_file(path), digest)
        self.assertEqual(len(self.fixture.projects.list_project_versions(
            self.fixture.token, self.fixture.project_id)), 2)

    def test_legacy_empty_plan_can_save_again_from_verified_same_version_evidence(self):
        _, original = self._payloads()
        write = workspace_module.write_json_atomic

        def legacy_writer(path, value):
            # Recreate the old writer's valid frozen artifacts in this anonymous
            # fixture; the ordinary pipeline computes every manifest hash.
            value = deepcopy(value)
            if Path(path).name == "script_plan.json":
                value["decisions"] = []
            elif Path(path).name in {"script_draft.json", "visual_binding.json"}:
                value["decisions"] = deepcopy(original["script_draft"]["decisions"])
            return write(path, value)

        def save_sound(value, key):
            current = self.workspace.read_current(self.fixture.token, **self.scope)
            changed = self.workspace.apply_table_change(
                self.fixture.token, **self.scope, expected_project_version=current["project_version"],
                expected_revision=current["revision"], shot_no="3", shot_id=current["shots"][2]["shot_id"],
                field="sound", value=value, idempotency_key=key + "-edit",
            )
            return self.workspace.save_version(
                self.fixture.token, **self.scope, expected_project_version=current["project_version"],
                expected_revision=changed["revision"], reason="调整第三镜声音", idempotency_key=key,
            )

        with patch.object(workspace_module, "write_json_atomic", side_effect=legacy_writer):
            self.assertEqual(save_sound("掌声收束", "legacy-save")["save_status"], "completed")
        paths, legacy = self._payloads()
        self.assertEqual(legacy["script_plan"]["decisions"], [])
        self.assertEqual(len(legacy["script_draft"]["decisions"]), 17)
        hashes = {path: sha256_file(path) for path in paths.values()}
        self.assertEqual(save_sound("掌声收束后保留音乐", "recovered-save")["save_status"], "completed")
        _, recovered = self._payloads()
        for payload in recovered.values():
            self.assertEqual(payload["decisions"], original["script_plan"]["decisions"])
        self.assertIn("recovered_frozen_decisions", recovered["script_plan"]["manual_revision"])
        for path, digest in hashes.items():
            self.assertEqual(sha256_file(path), digest)

    def test_saved_first_middle_last_deletions_project_every_artifact_and_adjacent_pair(self):
        token = self.fixture.token
        for position in ("first", "middle", "last"):
            with self.subTest(position=position):
                paths, before = self._payloads()
                hashes = {path: sha256_file(path) for path in paths.values()}
                current = self.workspace.read_current(token, **self.scope)
                shots = current["shots"]
                index = 0 if position == "first" else len(shots)-1 if position == "last" else len(shots)//2
                changed = self.workspace.apply_revision_operation(
                    token, **self.scope, expected_project_version=current["project_version"],
                    expected_revision=current["revision"],
                    operation={"operation": "delete_shot", "shot_no": shots[index]["shot_no"], "shot_id": shots[index]["shot_id"]},
                    idempotency_key=f"delete-{position}",
                )
                proposal = self.fixture.revision.propose_draft_save(
                    token, project_id=self.fixture.project_id, expected_project_version=current["project_version"],
                    expected_revision=changed["revision"], reason="删除指定镜头并保留其他镜头", idempotency_key=f"save-{position}",
                )
                result = self.fixture.agent.confirm_action(token, proposal["task_id"], proposal_digest=proposal["response"]["proposal_digest"])
                self.assertEqual(result["task"]["status"], "completed", result["response"])
                _, after = self._payloads()
                expected_numbers = [str(i) for i in range(1, len(shots))]
                for name, payload in after.items():
                    self.assertEqual([row["镜号"] for row in payload["script_rows"]], expected_numbers, name)
                    self.assertEqual([row["slot"]["shot_no"] for row in payload["decisions"]], expected_numbers, name)
                    expected_sources = [item["variant"] for i, item in enumerate(before[name]["decisions"]) if i != index]
                    self.assertEqual([item["variant"] for item in payload["decisions"]], expected_sources, name)
                    self.assertEqual([row["shot_no"] for row in payload["template_slots"]], expected_numbers, name)
                _, context = self.workspace._current_context(token, **self.scope)
                workbook = self.fixture.base.resolver.resolve_artifact(context.run_id, "workbook").path
                book = load_workbook(workbook)
                try:
                    self.assertEqual(len(read_managed_sheet_rows(book, ADJACENT_REVIEW_SHEET_NAME)), len(shots)-2)
                finally:
                    book.close()
                for path, digest in hashes.items():
                    self.assertEqual(sha256_file(path), digest)


if __name__ == "__main__":
    unittest.main()
