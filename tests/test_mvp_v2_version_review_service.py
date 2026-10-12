from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mvp_v2.artifact_service import ArtifactResolutionError, RunArtifactResolver
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.version_quality import (
    DomainFinalConfirmationCommand,
    SCHEMA_VERSION,
    VersionQualityService,
)
from mvp_v2.version_review_service import InMemoryVersionReviewRepository, VersionReviewError, VersionReviewService
from mvp_v2.workspace_store import WorkspaceStore


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _shots() -> list[dict]:
    return [
        {"shot_no": "1", "fields": {"copy": "开场", "image_id": "image_a"}, "source_refs": [{"source_file": "brief.txt", "source_sheet": "", "source_row": "1"}]},
        {"shot_no": "2", "fields": {"copy": "收束", "image_id": "image_b"}, "source_refs": [{"source_file": "brief.txt", "source_sheet": "", "source_row": "2"}]},
    ]


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QualityFixture:
    """Anonymous isolated fixture; no real data root, publication, or CURRENT."""

    def __init__(self, root: Path):
        self.root = root
        self.runs_root = root / "runs"
        store = WorkspaceStore(root / "workspace.sqlite3")
        identity = LocalIdentityAdapter(store)
        owner = identity.bootstrap_owner(
            workspace_name="匿名工作区", display_name="匿名管理员", confirmed=True,
            token="anonymous-quality-owner-token",
        )
        self.auth_token = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.projects = ProjectService(store)
        self.project = self.projects.create_project(
            self.auth_token, workspace_id=self.workspace_id, name="匿名质量项目",
            category_id="launch", expected_workspace_version=0,
        )
        self.run_id = "run_anon_0001"
        run_dir = self.runs_root / "anonymous" / "runs" / self.run_id
        run_dir.mkdir(parents=True)
        self.report = run_dir / "run_report.json"
        self.report.write_text(json.dumps({"run_id": self.run_id, "machine_audit_status": "pass"}), encoding="utf-8")
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(json.dumps({
            "run_id": self.run_id,
            "artifacts": {"run_report": {"path": "run_report.json", "sha256": _file_sha(self.report)}},
        }, sort_keys=True), encoding="utf-8")
        bound = self.projects.bind_run(
            self.auth_token, project_id=self.project["project_id"], run_id=self.run_id,
            parent_run_id=None, source_kind="native",
            manifest_ref=f"artifact://run/{self.run_id}/manifest",
            manifest_sha256=_file_sha(manifest),
            expected_project_version=self.projects.get_project(
                self.auth_token, self.project["project_id"]
            )["project_version"],
        )
        self.version = self.projects.create_project_version(
            self.auth_token, project_id=self.project["project_id"], run_id=self.run_id,
            parent_version_id=None, content_sha256=_sha("anonymous-version"),
            artifact_ref=f"artifact://run/{self.run_id}/run_report",
            expected_project_version=bound["project_version"],
        )
        self.reviews = VersionReviewService(InMemoryVersionReviewRepository())
        draft = self.reviews.create_draft(
            workspace_id=self.workspace_id, project_id=self.project["project_id"],
            base_version_id="version_seed", shots=_shots(), actor_id="actor_editor",
        )
        self.reviews.submit_draft(
            draft.draft_id, actor_id="actor_editor",
            formal_version_id=self.version["version_id"],
        )
        self.quality = VersionQualityService(
            projects=self.projects, artifacts=RunArtifactResolver(self.runs_root), reviews=self.reviews,
        )


class VersionReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryVersionReviewRepository()
        self.service = VersionReviewService(self.repository)
        self.draft = self.service.create_draft(
            workspace_id="workspace_anonymous", project_id="project_anonymous",
            base_version_id="version_base", shots=_shots(), actor_id="actor_editor",
        )

    def _duration_draft(self, *, creative=False):
        shots = _shots()
        for shot in shots:
            shot["fields"]["时长"] = "建议5秒" if creative else "5秒"
            if creative:
                shot["fields"]["段落"] = "匿名段落"
        return self.service.create_draft(
            workspace_id="workspace_anonymous", project_id="project_anonymous",
            base_version_id=None if creative else "version_base",
            shots=shots, actor_id="actor_editor",
        )

    def test_invalid_duration_keeps_formal_draft_history_and_checkpoints_unchanged(self):
        draft = self._duration_draft()
        for apply in (self.service.apply_table_patch, self.service.apply_agent_patch):
            for value in (-1, 0, "-3秒", "0s", "3.5s", "随便", "", None, "待定", True):
                with self.subTest(origin=apply.__name__, value=value):
                    before = self.repository.draft_state(draft.draft_id)
                    checkpoints = self.repository.checkpoints(draft.draft_id)
                    with self.assertRaisesRegex(VersionReviewError, "时长"):
                        apply(draft.draft_id, expected_revision=0, actor_id="actor_editor",
                              shot_no="1", field="时长", value=value)
                    self.assertEqual(self.repository.draft_state(draft.draft_id), before)
                    self.assertEqual(self.repository.checkpoints(draft.draft_id), checkpoints)
        saved, _ = self.service.apply_table_patch(
            draft.draft_id, expected_revision=0, actor_id="actor_editor",
            shot_no="1", field="时长", value="8s",
        )
        self.assertEqual(saved.revision, 1)
        restored = self.service.undo(draft.draft_id, expected_revision=1, actor_id="actor_editor")
        self.assertEqual(restored.shots[0]["fields"]["时长"], "5秒")

    def test_creative_duration_can_be_empty_pending_or_positive_suggestion(self):
        draft = self._duration_draft(creative=True)
        for value in ("", "待定", "建议6秒", "6s"):
            with self.subTest(value=value):
                draft, patch = self.service.apply_table_patch(
                    draft.draft_id, expected_revision=draft.revision, actor_id="actor_editor",
                    shot_no="1", field="时长", value=value,
                )
                self.assertEqual(draft.shots[0]["fields"]["时长"], value)
                self.assertEqual(patch.new_value, value)
        for value in ("建议-3秒", "建议0秒", "建议3.5秒", "不能拍摄", True):
            with self.subTest(value=value):
                before = self.repository.draft_state(draft.draft_id)
                with self.assertRaisesRegex(VersionReviewError, "时长"):
                    self.service.apply_table_patch(
                        draft.draft_id, expected_revision=draft.revision, actor_id="actor_editor",
                        shot_no="1", field="时长", value=value,
                    )
                self.assertEqual(self.repository.draft_state(draft.draft_id), before)
        draft, patches = self.service.apply_agent_change_set(
            draft.draft_id, expected_revision=draft.revision, actor_id="actor_agent",
            operations=[
                {"shot_no": "1", "field": "时长", "value": "待定"},
                {"shot_no": "2", "field": "时长", "value": ""},
            ],
        )
        self.assertEqual([shot["fields"]["时长"] for shot in draft.shots], ["待定", ""])
        self.assertEqual(len(patches), 2)

    def test_invalid_duration_rolls_back_entire_agent_change_set(self):
        for creative in (False, True):
            draft = self._duration_draft(creative=creative)
            for value in ("-3秒", "0s", "3.5s", "随便"):
                with self.subTest(creative=creative, value=value):
                    before = self.repository.draft_state(draft.draft_id)
                    checkpoints = self.repository.checkpoints(draft.draft_id)
                    with self.assertRaisesRegex(VersionReviewError, "时长"):
                        self.service.apply_agent_change_set(
                            draft.draft_id, expected_revision=0, actor_id="actor_agent",
                            operations=[
                                {"shot_no": "1", "field": "copy", "value": "不应单独写入的文案"},
                                {"shot_no": "2", "field": "时长", "value": value},
                            ],
                        )
                    self.assertEqual(self.repository.draft_state(draft.draft_id), before)
                    self.assertEqual(self.repository.checkpoints(draft.draft_id), checkpoints)

    def test_table_and_agent_share_one_draft_with_lock_undo_redo_and_submit_seal(self):
        locked = self.service.set_locks(self.draft.draft_id, entries=[("2", "copy")], actor_id="actor_editor")
        self.assertEqual(locked.entries, (("2", "copy"),))
        table, patch = self.service.apply_table_patch(self.draft.draft_id, expected_revision=0, actor_id="actor_editor", shot_no="1", field="copy", value="新开场")
        self.assertEqual(patch.origin, "table")
        with self.assertRaisesRegex(VersionReviewError, "已锁定"):
            self.service.apply_agent_patch(self.draft.draft_id, expected_revision=table.revision, actor_id="actor_agent", shot_no="2", field="copy", value="不可变")
        agent, agent_patch, formal = self.service.apply_agent_patch(self.draft.draft_id, expected_revision=table.revision, actor_id="actor_agent", shot_no="1", field="image_id", value="image_new", save_version=False)
        self.assertEqual(agent_patch.origin, "natural_language")
        with self.assertRaisesRegex(VersionReviewError, "actor_id"):
            self.service.undo(self.draft.draft_id, expected_revision=agent.revision, actor_id="")
        undone = self.service.undo(self.draft.draft_id, expected_revision=agent.revision, actor_id="actor_editor")
        self.assertEqual(undone.shots[0]["fields"]["image_id"], "image_a")
        redone = self.service.redo(self.draft.draft_id, expected_revision=undone.revision, actor_id="actor_editor")
        self.assertEqual(redone.shots[0]["fields"]["image_id"], "image_new")
        self.assertEqual(
            self.repository.checkpoints(self.draft.draft_id)[-1].actor_id,
            "actor_editor",
        )
        formal = self.service.submit_draft(self.draft.draft_id, actor_id="actor_agent")
        self.assertEqual(formal.parent_version_id, "version_base")
        with self.assertRaisesRegex(VersionReviewError, "不可覆盖"):
            self.service.apply_table_patch(self.draft.draft_id, expected_revision=redone.revision, actor_id="actor_editor", shot_no="1", field="copy", value="覆盖")
        next_draft = self.service.create_followup_draft(formal.version_id, actor_id="actor_editor")
        self.assertEqual(next_draft.base_version_id, formal.version_id)

    def test_source_triples_and_concurrent_revision_cannot_regress(self):
        with self.assertRaisesRegex(VersionReviewError, "来源三元组"):
            self.service.apply_table_patch(self.draft.draft_id, expected_revision=0, actor_id="actor_editor", shot_no="1", field="source_refs", value=[])
        first, _ = self.service.apply_table_patch(self.draft.draft_id, expected_revision=0, actor_id="actor_editor", shot_no="1", field="copy", value="改过")
        with self.assertRaisesRegex(VersionReviewError, "DRAFT_REVISION_CONFLICT"):
            self.service.apply_table_patch(self.draft.draft_id, expected_revision=0, actor_id="actor_other", shot_no="1", field="copy", value="并发覆盖")
        self.assertEqual(first.shots[0]["source_refs"][0]["source_row"], "1")

    def test_agent_batch_is_one_revision_and_invalid_target_writes_nothing(self):
        changed, patches = self.service.apply_agent_change_set(
            self.draft.draft_id,
            expected_revision=0,
            actor_id="actor_agent",
            operations=[
                {"shot_no": "1", "field": "copy", "value": "更自然的开场"},
                {"shot_no": "2", "field": "copy", "value": "更简洁的收束"},
            ],
        )
        self.assertEqual(changed.revision, 1)
        self.assertEqual({patch.revision for patch in patches}, {1})
        self.assertEqual(
            [item["fields"]["copy"] for item in changed.shots],
            ["更自然的开场", "更简洁的收束"],
        )
        before, history, cursor = self.repository.draft_state(self.draft.draft_id)
        with self.assertRaisesRegex(VersionReviewError, "不存在"):
            self.service.apply_agent_change_set(
                self.draft.draft_id,
                expected_revision=1,
                actor_id="actor_agent",
                operations=[
                    {"shot_no": "1", "field": "copy", "value": "不应落库"},
                    {"shot_no": "9", "field": "copy", "value": "未知镜头"},
                ],
            )
        after, after_history, after_cursor = self.repository.draft_state(self.draft.draft_id)
        self.assertEqual(after, before)
        self.assertEqual((after_history, after_cursor), (history, cursor))

    def test_review_layers_batch_return_recheck_and_final_confirmation_are_immutable(self):
        formal = self.service.submit_draft(self.draft.draft_id, actor_id="actor_editor")
        image, problem, second_image = self.service.add_review_items(formal.version_id, items=[
            {"kind": "image", "shot_no": "1", "severity": "blocker", "evidence_ref": "artifact://review/image-1", "evidence_sha256": _sha("image-1")},
            {"kind": "script", "shot_no": "2", "severity": "attention", "evidence_ref": "artifact://review/script-2", "evidence_sha256": _sha("script-2")},
            {"kind": "image", "shot_no": "2", "severity": "attention", "evidence_ref": "artifact://review/image-2", "evidence_sha256": _sha("image-2")},
        ])
        returned = self.service.decide_review_items(formal.version_id, item_ids=[problem.review_item_id], decision="return", evidence_ref="artifact://review/return", evidence_sha256=_sha("return"), reviewer_actor_id="actor_manager")
        self.assertEqual(returned[0].decision, "return")
        recheck = self.service.focus_recheck(
            formal.version_id, item_ids=[problem.review_item_id],
            evidence_ref="artifact://review/recheck", evidence_sha256=_sha("recheck"),
            reviewer_actor_id="actor_manager",
        )
        self.assertEqual(recheck[0].decision, "recheck")
        approved = self.service.decide_review_items(formal.version_id, item_ids=[image.review_item_id, problem.review_item_id, second_image.review_item_id], decision="approve", evidence_ref="artifact://review/approve", evidence_sha256=_sha("approve"), reviewer_actor_id="actor_manager")
        self.assertEqual(len(approved), 3)
        snapshot = self.service.shot_review_snapshot("workspace_anonymous", "project_anonymous", formal.version_id)
        self.assertEqual((snapshot.total_count, snapshot.resolved_count, snapshot.unresolved_count), (2, 2, 0))
        command = DomainFinalConfirmationCommand(self.service)
        confirmation = command.execute(version_id=formal.version_id, decision="quality_pass", evidence_ref="artifact://review/final", evidence_sha256=_sha("final"), reviewer_actor_id="actor_manager")
        self.assertEqual(confirmation.decision, "quality_pass")
        with self.assertRaisesRegex(VersionReviewError, "不可再决策"):
            self.service.decide_review_items(formal.version_id, item_ids=[image.review_item_id], decision="approve", evidence_ref="artifact://review/again", evidence_sha256=_sha("again"), reviewer_actor_id="actor_manager")

    def test_final_pass_requires_n_image_confirmations_and_no_unresolved_shot_or_whole_item(self):
        formal = self.service.submit_draft(self.draft.draft_id, actor_id="actor_editor")
        command = DomainFinalConfirmationCommand(self.service)
        zero = self.service.shot_review_snapshot("workspace_anonymous", "project_anonymous", formal.version_id)
        self.assertEqual((zero.total_count, zero.resolved_count, zero.unresolved_count), (2, 0, 2))
        self.assertTrue(zero.snapshot_ref.startswith("memory://"))
        with self.assertRaisesRegex(VersionReviewError, "未解决"):
            command.execute(version_id=formal.version_id, decision="quality_pass", evidence_ref="artifact://review/final", evidence_sha256=_sha("final"), reviewer_actor_id="actor_manager")

        image_one, shot_one_problem, image_two, whole_problem = self.service.add_review_items(formal.version_id, items=[
            {"kind": "image", "shot_no": "1", "severity": "blocker", "evidence_ref": "artifact://review/image-1", "evidence_sha256": _sha("image-1")},
            {"kind": "script", "shot_no": "1", "severity": "attention", "evidence_ref": "artifact://review/shot-1", "evidence_sha256": _sha("shot-1")},
            {"kind": "image", "shot_no": "2", "severity": "blocker", "evidence_ref": "artifact://review/image-2", "evidence_sha256": _sha("image-2")},
            {"kind": "script", "shot_no": None, "severity": "blocker", "evidence_ref": "artifact://review/whole", "evidence_sha256": _sha("whole")},
        ])
        self.service.decide_review_items(
            formal.version_id, item_ids=[image_one.review_item_id], decision="approve",
            evidence_ref="artifact://review/approve-1", evidence_sha256=_sha("approve-1"), reviewer_actor_id="actor_manager",
        )
        missing_image = self.service.shot_review_snapshot("workspace_anonymous", "project_anonymous", formal.version_id)
        self.assertEqual(missing_image.total_count, 2)
        self.assertEqual(missing_image.resolved_count, 0)
        self.assertGreater(missing_image.unresolved_count, 0)
        self.service.decide_review_items(
            formal.version_id, item_ids=[image_two.review_item_id, shot_one_problem.review_item_id], decision="approve",
            evidence_ref="artifact://review/approve-shots", evidence_sha256=_sha("approve-shots"), reviewer_actor_id="actor_manager",
        )
        self.service.decide_review_items(
            formal.version_id, item_ids=[whole_problem.review_item_id], decision="return",
            evidence_ref="artifact://review/return-whole", evidence_sha256=_sha("return-whole"), reviewer_actor_id="actor_manager",
        )
        self.service.focus_recheck(
            formal.version_id, item_ids=[whole_problem.review_item_id],
            evidence_ref="artifact://review/recheck-whole", evidence_sha256=_sha("recheck-whole"), reviewer_actor_id="actor_manager",
        )
        whole_unresolved = self.service.shot_review_snapshot("workspace_anonymous", "project_anonymous", formal.version_id)
        self.assertEqual((whole_unresolved.total_count, whole_unresolved.resolved_count), (2, 2))
        self.assertGreater(whole_unresolved.unresolved_count, 0)
        with self.assertRaisesRegex(VersionReviewError, "未解决"):
            command.execute(version_id=formal.version_id, decision="quality_pass", evidence_ref="artifact://review/final", evidence_sha256=_sha("final"), reviewer_actor_id="actor_manager")
        self.service.decide_review_items(
            formal.version_id, item_ids=[whole_problem.review_item_id], decision="approve",
            evidence_ref="artifact://review/approve-whole", evidence_sha256=_sha("approve-whole"), reviewer_actor_id="actor_manager",
        )
        complete = self.service.shot_review_snapshot("workspace_anonymous", "project_anonymous", formal.version_id)
        self.assertEqual((complete.total_count, complete.resolved_count, complete.unresolved_count), (2, 2, 0))
        self.assertEqual(complete.image_confirmed_shot_nos, ("1", "2"))
        confirmation = command.execute(version_id=formal.version_id, decision="quality_pass", evidence_ref="artifact://review/final", evidence_sha256=_sha("final"), reviewer_actor_id="actor_manager")
        self.assertEqual(confirmation.decision, "quality_pass")
        with self.assertRaises(TypeError):
            command.execute(version_id=formal.version_id, decision="quality_fail", evidence_ref="artifact://review/final", evidence_sha256=_sha("final"), reviewer_actor_id="actor_manager", agent_session_id="agent_forbidden")


class VersionQualityEvidenceTests(unittest.TestCase):
    def test_resolver_derives_authoritative_evidence_and_keeps_three_stage_gates_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = QualityFixture(Path(tmp))
            version_id = fixture.version["version_id"]
            project_id = fixture.project["project_id"]
            before = fixture.quality.resolve(fixture.auth_token, project_id=project_id, version_id=version_id)
            self.assertEqual(before.schema_version, SCHEMA_VERSION)
            self.assertEqual(before.machine_audit.status, "pass")
            self.assertEqual(before.machine_audit.ref, f"artifact://run/{fixture.run_id}/run_report")
            self.assertTrue(fixture.quality._display_can_prepare_image_candidates(before))
            self.assertFalse(fixture.quality._display_can_bind_image(before, shot_no="1"))
            self.assertFalse(fixture.quality._display_can_export_delivery(before))
            with self.assertRaises(TypeError):
                fixture.quality.resolve(fixture.auth_token, project_id=project_id, version_id=version_id, machine_audit={})
            fixture.report.write_text(json.dumps({"run_id": fixture.run_id, "machine_audit_status": "fail"}), encoding="utf-8")
            with self.assertRaisesRegex(ArtifactResolutionError, "HASH_MISMATCH"):
                fixture.quality.resolve(fixture.auth_token, project_id=project_id, version_id=version_id)
            fixture.report.write_text(json.dumps({"run_id": fixture.run_id, "machine_audit_status": "pass"}), encoding="utf-8")

            items = fixture.reviews.add_review_items(version_id, items=[
                {"kind": "image", "shot_no": "1", "severity": "attention", "evidence_ref": "artifact://review/shot-1", "evidence_sha256": _sha("shot-1")},
                {"kind": "image", "shot_no": "2", "severity": "attention", "evidence_ref": "artifact://review/shot-2", "evidence_sha256": _sha("shot-2")},
            ])
            fixture.reviews.decide_review_items(
                version_id, item_ids=[item.review_item_id for item in items], decision="approve",
                evidence_ref="artifact://review/shot-approve", evidence_sha256=_sha("shot-approve"),
                reviewer_actor_id="actor_reviewer",
            )
            reviewed = fixture.quality.resolve(fixture.auth_token, project_id=project_id, version_id=version_id)
            self.assertTrue(fixture.quality._display_can_bind_image(reviewed, shot_no="1"))
            self.assertFalse(fixture.quality._display_can_export_delivery(reviewed))
            DomainFinalConfirmationCommand(fixture.reviews).execute(
                version_id=version_id, decision="quality_pass", evidence_ref="artifact://review/final",
                evidence_sha256=_sha("final"), reviewer_actor_id="actor_reviewer",
            )
            final = fixture.quality.resolve(fixture.auth_token, project_id=project_id, version_id=version_id)
            self.assertEqual(final.shot_review.unresolved_count, 0)
            self.assertEqual(final.final_confirmation.decision, "quality_pass")
            self.assertTrue(fixture.quality._display_can_export_delivery(final))


if __name__ == "__main__":
    unittest.main()
