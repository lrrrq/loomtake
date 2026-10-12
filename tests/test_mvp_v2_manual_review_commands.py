from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import sqlite3

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.identity_service import LocalIdentityAdapter, require_permission
from mvp_v2.manual_review_commands import ManualReviewCommandService, ManualReviewCommandServices
from mvp_v2.review_evidence_artifacts import FileReviewEvidenceArtifactStore, ReviewEvidenceAuthority
from mvp_v2.project_service import ProjectService
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_quality import AuthenticatedFinalConfirmationService
from mvp_v2.version_quality_artifacts import FileVersionQualityArtifactStore
from mvp_v2.version_quality_factory import build_workspace_version_quality_resolver
from mvp_v2.version_review_repository import WorkspaceVersionReviewRepository
from mvp_v2.version_review_service import VersionReviewService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore
from mvp_v2.agent_tools import TOOL_REGISTRY


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Actor:
    def __init__(self, store):
        self.store = store

    def resolve_reviewer(self, token, *, workspace_id, project_id):
        with self.store.connection() as connection:
            return require_permission(
                connection, token, workspace_id=workspace_id, project_id=project_id,
                permission="review.record",
            )["actor_id"]


class Fixture:
    def __init__(self, root: Path):
        self.root, self.store = root, WorkspaceStore(root / "workspace.sqlite3")
        owner = LocalIdentityAdapter(self.store).bootstrap_owner(
            workspace_name="匿名审核工作区", display_name="匿名审核员",
            confirmed=True, token="manual-review-token",
        )
        self.token, self.workspace_id = owner["auth_token"], owner["workspace_id"]
        self.projects = ProjectService(self.store)
        workspace = self.projects.get_workspace(self.token, self.workspace_id)
        self.project = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名审核项目",
            category_id="launch", expected_workspace_version=workspace["workspace_version"],
        )
        self.run_id = "run_manual_review_0001"
        run = root / "runs" / "anonymous" / "runs" / self.run_id
        run.mkdir(parents=True)
        rows = [{"镜号": "1"}, {"镜号": "2"}]
        details = [{"模板镜号": "1"}, {"模板镜号": "2"}]
        stages = {}
        for name, schema in (
            ("script_plan", "mplus-script-plan/v2"),
            ("script_draft", "mplus-script-draft/v1"),
            ("visual_binding", "mplus-visual-binding/v1"),
        ):
            payload = {"schema_version": schema, "script_rows": rows, "detail_rows": details}
            if name == "visual_binding":
                payload["bindings"] = [{"shot_no": "1"}, {"shot_no": "2"}]
            path = run / f"{name}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            stages[name] = path
        workbook, report = run / "workbook.xlsx", run / "run_report.json"
        workbook.write_bytes(b"anonymous workbook")
        report.write_text(json.dumps({"run_id": self.run_id, "machine_audit_status": "pass"}), encoding="utf-8")
        manifest = run / "run_manifest.json"
        manifest.write_text(json.dumps({
            "run_id": self.run_id,
            "artifacts": {
                "workbook": {"path": "workbook.xlsx", "sha256": _sha(workbook)},
                "run_report": {"path": "run_report.json", "sha256": _sha(report)},
            },
            "stages": {name: {"stage_id": name, "status": "succeeded",
                        "output_refs": [str(path.resolve())], "output_sha256": _sha(path)}
                       for name, path in stages.items()},
        }, sort_keys=True), encoding="utf-8")
        bound = self.projects.bind_run(
            self.token, project_id=self.project["project_id"], run_id=self.run_id,
            source_kind="native", manifest_ref=f"artifact://run/{self.run_id}/manifest",
            manifest_sha256=_sha(manifest), expected_project_version=self.project["project_version"],
        )
        self.version = self.projects.create_project_version(
            self.token, project_id=self.project["project_id"], run_id=self.run_id,
            content_sha256=_sha(workbook), artifact_ref=f"artifact://run/{self.run_id}/workbook",
            expected_project_version=bound["project_version"],
        )
        self.delivery = VersionDeliveryService(self.store, RunArtifactResolver(root / "runs"))
        bootstrap_context = self.delivery.resolve(
            self.token, self.project["project_id"], self.version["version_id"],
            expected_project_version=self.version["project_version"],
        )
        review_repo = WorkspaceVersionReviewRepository(
            self.store, auth_token=self.token, workspace_id=bootstrap_context.workspace_id,
            project_id=bootstrap_context.project_id, version_id=bootstrap_context.version_id,
            run_id=bootstrap_context.run_id, expected_project_lock_version=bootstrap_context.project_lock_version,
        )
        self.review = VersionReviewService(review_repo)
        self.quality = build_workspace_version_quality_resolver(
            store=self.store, delivery_service=self.delivery,
            run_artifacts=RunArtifactResolver(root / "runs"), reviews=self.review,
            evidence_artifacts=FileVersionQualityArtifactStore(root / "quality-evidence"),
        )
        self.command = ManualReviewCommandService(ManualReviewCommandServices(
            store=self.store, delivery=self.delivery, quality=self.quality,
            final_confirmation=AuthenticatedFinalConfirmationService(
                resolver=self.quality, actor_resolver=_Actor(self.store),
            ),
            evidence_authority=ReviewEvidenceAuthority(
                FileReviewEvidenceArtifactStore(root / "review-evidence")
            ),
        ))

    @property
    def lock(self):
        return self.projects.get_project(self.token, self.project["project_id"])["project_version"]

    def payload(self, key, **more):
        return {
            "project_id": self.project["project_id"], "version_id": self.version["version_id"],
            "expected_project_lock_version": self.lock, "idempotency_key": key, **more,
        }

    @staticmethod
    def shots():
        return [
            {"shot_no": "1", "fields": {"copy": "一", "image_id": "image_1"},
             "source_refs": [{"source_file": "anon", "source_sheet": "brief", "source_row": "1"}]},
            {"shot_no": "2", "fields": {"copy": "二", "image_id": "image_2"},
             "source_refs": [{"source_file": "anon", "source_sheet": "brief", "source_row": "2"}]},
        ]

    def execute(self, action, parameters):
        prepared = self.command.prepare(self.token, action=action, parameters=parameters)
        return self.command.execute(
            self.token, action=action, parameters=parameters, command_token=prepared["command_token"]
        )["result"]


class ManualReviewCommandTests(unittest.TestCase):
    def test_generic_manual_review_commands_allow_multiple_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            first = fixture.execute("create_draft", fixture.payload("generic-create-1", shots=fixture.shots()))
            second = fixture.execute("create_draft", fixture.payload("generic-create-2", shots=fixture.shots()))
            self.assertNotEqual(first["draft_id"], second["draft_id"])
            with fixture.store.connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM version_review_drafts").fetchone()[0],
                    2,
                )

    def test_persistent_draft_table_and_natural_language_share_state_then_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            created = fixture.execute("create_draft", fixture.payload("create-1", shots=fixture.shots()))
            table = fixture.execute("apply_change_set", fixture.payload(
                "table-1", draft_id=created["draft_id"], expected_revision=0,
                origin="table", shot_no="1", field="copy", value="表格修改",
            ))
            natural = fixture.execute("apply_change_set", fixture.payload(
                "natural-1", draft_id=created["draft_id"], expected_revision=1,
                origin="natural_language", shot_no="2", field="copy", value="自然语言修改",
            ))
            undone = fixture.execute("undo", fixture.payload(
                "undo-1", draft_id=created["draft_id"], expected_revision=2,
            ))
            redone = fixture.execute("redo", fixture.payload(
                "redo-1", draft_id=created["draft_id"], expected_revision=3,
            ))
            self.assertEqual(
                (table["revision"], natural["revision"], undone["revision"], redone["revision"]),
                (1, 2, 3, 4),
            )
            context = fixture.delivery.resolve(
                fixture.token, fixture.project["project_id"], fixture.version["version_id"],
                expected_project_version=fixture.lock,
            )
            reopened = WorkspaceVersionReviewRepository(
                WorkspaceStore(fixture.store.database_path), auth_token=fixture.token,
                workspace_id=context.workspace_id, project_id=context.project_id,
                version_id=context.version_id, run_id=context.run_id,
                expected_project_lock_version=context.project_lock_version,
            )
            draft, _, cursor = reopened.draft_state(created["draft_id"])
            self.assertEqual((draft.revision, cursor, draft.shots[1]["fields"]["copy"]), (4, 2, "自然语言修改"))
            with fixture.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_publications").fetchone()[0], 0)
            self.assertFalse((fixture.root / "CURRENT").exists())

    def test_lock_cas_cross_project_and_employee_dto_information_reduction(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            created = fixture.execute("create_draft", fixture.payload("create-2", shots=fixture.shots()))
            fixture.execute("set_lock_set", fixture.payload(
                "locks-1", draft_id=created["draft_id"], entries=[["1", "copy"]],
            ))
            with self.assertRaisesRegex(Exception, "锁定"):
                fixture.execute("apply_change_set", fixture.payload(
                    "locked-1", draft_id=created["draft_id"], expected_revision=0,
                    origin="table", shot_no="1", field="copy", value="拒绝",
                ))
            with self.assertRaisesRegex(Exception, "DRAFT_REVISION_CONFLICT"):
                fixture.execute("apply_change_set", fixture.payload(
                    "stale-1", draft_id=created["draft_id"], expected_revision=8,
                    origin="natural_language", shot_no="2", field="copy", value="拒绝",
                ))
            items = fixture.execute("create_review_items", fixture.payload(
                "items-1", shots=fixture.shots(),
                items=[{"kind": "image", "shot_no": "1", "severity": "blocker", "note": "需确认图片"}],
            ))
            fixture.execute("decide_review_items", fixture.payload(
                "decision-1", item_keys=items["issue_keys"], decision="approve", note="图片已确认",
            ))
            model = fixture.command.read_model(
                fixture.token, project_id=fixture.project["project_id"],
                version_id=fixture.version["version_id"], expected_project_lock_version=fixture.lock,
            )
            raw = json.dumps(model, ensure_ascii=False)
            self.assertNotIn("artifact://", raw)
            self.assertNotIn("sha256", raw.lower())
            self.assertNotIn("actor_id", raw)
            self.assertNotIn("reviewer_actor_id", raw)
            self.assertNotIn("confirmation_id", raw)
            self.assertNotIn("publication", raw.lower())
            self.assertNotIn("CURRENT", raw)
            self.assertTrue({"issue_key", "kind", "shot_no", "severity", "status"} <= set(model["issues"][0]))
            self.assertTrue({"decision_key", "issue_key", "decision", "reviewer_label", "decided_at", "evidence_status"} <= set(model["decisions"][0]))
            self.assertEqual(model["issues"][0]["status"], "approve")
            with fixture.store.transaction() as connection:
                item_id = fixture.review.repository.review_items(fixture.version["version_id"])[0].review_item_id
                with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                    connection.execute(
                        "UPDATE version_review_items SET severity='attention' WHERE review_item_id=?",
                        (item_id,),
                    )

    def test_final_confirmation_uses_migration9_and_never_agent_or_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            items = fixture.execute("create_review_items", fixture.payload(
                "items-final", shots=fixture.shots(), items=[
                    {"kind": "image", "shot_no": "1", "severity": "blocker", "note": "镜头一图片"},
                    {"kind": "image", "shot_no": "2", "severity": "blocker", "note": "镜头二图片"},
                ],
            ))
            fixture.execute("decide_review_items", fixture.payload(
                "approve-final", item_keys=items["issue_keys"], decision="approve", note="全部图片确认",
            ))
            confirmed = fixture.execute("confirm_final", fixture.payload(
                "confirm-final", decision="quality_pass", note="整片人工确认",
            ))
            self.assertEqual(confirmed["decision"], "quality_pass")
            model = fixture.command.read_model(
                fixture.token, project_id=fixture.project["project_id"],
                version_id=fixture.version["version_id"], expected_project_lock_version=fixture.lock,
            )
            self.assertEqual(model["final_confirmation"]["decision"], "quality_pass")
            with fixture.store.connection() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM version_quality_final_confirmations"
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM version_publications"
                ).fetchone()[0], 0)
            self.assertFalse(set(_MANUAL_REVIEW_ACTIONS) & set(TOOL_REGISTRY))

    def test_client_evidence_spoof_is_rejected_before_decision_or_final_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            attacker = {"evidence_ref": "artifact://attacker/unregistered-proof", "evidence_sha256": "0" * 64}
            with self.assertRaisesRegex(WorkspaceServiceError, "不接受客户端证据"):
                fixture.command.prepare(fixture.token, action="confirm_final", parameters=fixture.payload(
                    "attacker-final", decision="quality_pass", note="伪造", **attacker,
                ))
            with self.assertRaisesRegex(WorkspaceServiceError, "不接受客户端证据"):
                fixture.command.prepare(fixture.token, action="confirm_final", parameters=fixture.payload(
                    "unknown-key", decision="quality_pass", note="伪造", evidence_key="unknown",
                ))
            with fixture.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_review_human_decisions").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_quality_final_confirmations").fetchone()[0], 0)

    def test_deleted_or_tampered_server_evidence_blocks_later_decision_and_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            items = fixture.execute("create_review_items", fixture.payload(
                "server-item", shots=fixture.shots(),
                items=[{"kind": "image", "shot_no": "1", "severity": "blocker", "note": "服务端证据"}],
            ))
            with fixture.store.connection() as connection:
                row = connection.execute("SELECT evidence_sha256 FROM version_review_items").fetchone()
            evidence_path = fixture.root / "review-evidence" / "artifacts" / "version-review-evidence" / row["evidence_sha256"][:2] / f"{row['evidence_sha256']}.json"
            evidence_path.unlink()
            with self.assertRaisesRegex(WorkspaceServiceError, "审核证据不存在"):
                fixture.execute("decide_review_items", fixture.payload(
                    "deleted-before-decision", item_keys=items["issue_keys"], decision="approve", note="不应写入",
                ))
            with fixture.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_review_human_decisions").fetchone()[0], 0)

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            items = fixture.execute("create_review_items", fixture.payload(
                "server-item-2", shots=fixture.shots(),
                items=[
                    {"kind": "image", "shot_no": "1", "severity": "blocker", "note": "一"},
                    {"kind": "image", "shot_no": "2", "severity": "blocker", "note": "二"},
                ],
            ))
            fixture.execute("decide_review_items", fixture.payload(
                "server-decision", item_keys=items["issue_keys"], decision="approve", note="已确认",
            ))
            with fixture.store.connection() as connection:
                row = connection.execute("SELECT evidence_sha256 FROM version_review_human_decisions LIMIT 1").fetchone()
            evidence_path = fixture.root / "review-evidence" / "artifacts" / "version-review-evidence" / row["evidence_sha256"][:2] / f"{row['evidence_sha256']}.json"
            evidence_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(WorkspaceServiceError, "审核证据字节已变化"):
                fixture.execute("confirm_final", fixture.payload(
                    "tampered-before-final", decision="quality_pass", note="不应写入",
                ))
            with fixture.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_quality_final_confirmations").fetchone()[0], 0)

    def test_current_version_lock_and_actor_are_rechecked_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            payload = fixture.payload("lock-actor", shots=fixture.shots())
            stale = {**payload, "expected_project_lock_version": payload["expected_project_lock_version"] + 1}
            with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
                fixture.command.prepare(fixture.token, action="create_draft", parameters=stale)
            with self.assertRaisesRegex(WorkspaceServiceError, "AUTH_SESSION_INVALID"):
                fixture.command.prepare("forged-token", action="create_draft", parameters=payload)
            with fixture.store.transaction() as connection:
                connection.execute(
                    "UPDATE projects SET current_version_id=NULL WHERE project_id=?",
                    (fixture.project["project_id"],),
                )
            with self.assertRaisesRegex(WorkspaceServiceError, "VERSION_NOT_CURRENT"):
                fixture.command.prepare(fixture.token, action="create_draft", parameters=payload)

    def test_cross_project_and_old_version_are_rejected_before_command_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            workspace = fixture.projects.get_workspace(fixture.token, fixture.workspace_id)
            other = fixture.projects.create_project(
                fixture.token, workspace_id=fixture.workspace_id, name="另一匿名项目",
                category_id="launch", expected_workspace_version=workspace["workspace_version"],
            )
            with self.assertRaises(WorkspaceServiceError):
                fixture.command.prepare(fixture.token, action="confirm_final", parameters={
                    "project_id": other["project_id"], "version_id": fixture.version["version_id"],
                    "expected_project_lock_version": other["project_version"], "idempotency_key": "cross-project",
                    "decision": "quality_pass", "note": "跨项目",
                })
            with fixture.store.transaction() as connection:
                connection.execute(
                    "UPDATE projects SET current_version_id=NULL WHERE project_id=?",
                    (fixture.project["project_id"],),
                )
            with self.assertRaisesRegex(WorkspaceServiceError, "VERSION_NOT_CURRENT"):
                fixture.command.prepare(fixture.token, action="confirm_final", parameters={
                    "project_id": fixture.project["project_id"], "version_id": fixture.version["version_id"],
                    "expected_project_lock_version": fixture.lock, "idempotency_key": "old-version",
                    "decision": "quality_pass", "note": "旧版本",
                })
            with fixture.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM manual_command_confirmations").fetchone()[0], 0)

    def test_server_evidence_reopens_from_content_addressed_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.execute("create_review_items", fixture.payload(
                "restart-evidence", shots=fixture.shots(),
                items=[{"kind": "image", "shot_no": "1", "severity": "blocker", "note": "重启读取"}],
            ))
            with fixture.store.connection() as connection:
                row = connection.execute(
                    "SELECT evidence_ref, evidence_sha256 FROM version_review_items"
                ).fetchone()
            reopened = FileReviewEvidenceArtifactStore(fixture.root / "review-evidence")
            payload = reopened.read_json(row["evidence_ref"], row["evidence_sha256"])
            self.assertEqual(
                (payload["workspace_id"], payload["project_id"], payload["version_id"], payload["run_id"]),
                (fixture.workspace_id, fixture.project["project_id"], fixture.version["version_id"], fixture.run_id),
            )

    def test_subject_shots_must_match_current_visual_binding_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            shortened = fixture.shots()[:1]
            with self.assertRaisesRegex(WorkspaceServiceError, "visual_binding"):
                fixture.execute("create_draft", fixture.payload("bad-shots", shots=shortened))


if __name__ == "__main__":
    unittest.main()


_MANUAL_REVIEW_ACTIONS = {
    "create_draft", "apply_change_set", "undo", "redo", "set_lock_set",
    "create_review_items", "decide_review_items", "focus_recheck", "confirm_final",
}
