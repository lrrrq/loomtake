from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_quality import AuthenticatedFinalConfirmationService
from mvp_v2.version_quality_artifacts import FileVersionQualityArtifactStore
from mvp_v2.version_quality_artifacts import VersionQualityArtifact
from mvp_v2.version_quality_factory import build_workspace_version_quality_resolver
from mvp_v2.version_quality_repository import WorkspaceVersionQualityRepository
from mvp_v2.version_review_service import InMemoryVersionReviewRepository, VersionReviewService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class QualityPersistenceFixture:
    def __init__(self, root: Path):
        self.root = root
        self.store = WorkspaceStore(root / "workspace.sqlite3")
        identity = LocalIdentityAdapter(self.store)
        owner = identity.bootstrap_owner(
            workspace_name="匿名质量工作区", display_name="匿名审核员",
            confirmed=True, token="quality-owner-token",
        )
        self.token, self.workspace_id = owner["auth_token"], owner["workspace_id"]
        self.projects = ProjectService(self.store)
        workspace = self.projects.get_workspace(self.token, self.workspace_id)
        self.project = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名质量项目",
            category_id="launch", expected_workspace_version=workspace["workspace_version"],
        )
        self.run_id = "run_quality_0001"
        run_dir = root / "runs" / "anonymous" / "runs" / self.run_id
        run_dir.mkdir(parents=True)
        rows = [{"镜号": "1"}, {"镜号": "2"}]
        details = [{"模板镜号": "1"}, {"模板镜号": "2"}]
        stages = {}
        for stage, schema in (
            ("script_plan", "mplus-script-plan/v2"),
            ("script_draft", "mplus-script-draft/v1"),
            ("visual_binding", "mplus-visual-binding/v1"),
        ):
            payload = {"schema_version": schema, "script_rows": rows, "detail_rows": details}
            if stage == "visual_binding":
                payload["bindings"] = [{"shot_no": "1"}, {"shot_no": "2"}]
            path = run_dir / f"{stage}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            stages[stage] = path
        workbook = run_dir / "workbook.xlsx"
        workbook.write_bytes(b"anonymous workbook")
        report = run_dir / "run_report.json"
        report.write_text(json.dumps({"run_id": self.run_id, "machine_audit_status": "pass"}), encoding="utf-8")
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(json.dumps({
            "run_id": self.run_id,
            "artifacts": {
                "workbook": {"path": "workbook.xlsx", "sha256": _sha(workbook)},
                "run_report": {"path": "run_report.json", "sha256": _sha(report)},
            },
            "stages": {
                key: {"stage_id": key, "status": "succeeded",
                      "output_refs": [str(path.resolve())], "output_sha256": _sha(path)}
                for key, path in stages.items()
            },
        }, sort_keys=True), encoding="utf-8")
        bound = self.projects.bind_run(
            self.token, project_id=self.project["project_id"], run_id=self.run_id,
            source_kind="native", manifest_ref=f"artifact://run/{self.run_id}/manifest",
            manifest_sha256=_sha(manifest),
            expected_project_version=self.project["project_version"],
        )
        self.version = self.projects.create_project_version(
            self.token, project_id=self.project["project_id"], run_id=self.run_id,
            content_sha256=_sha(workbook), artifact_ref=f"artifact://run/{self.run_id}/workbook",
            expected_project_version=bound["project_version"],
        )
        self.reviews = VersionReviewService(InMemoryVersionReviewRepository())
        draft = self.reviews.create_draft(
            workspace_id=self.workspace_id, project_id=self.project["project_id"],
            base_version_id="parent", actor_id=owner["actor_id"],
            shots=[
                {"shot_no": "1", "fields": {"image_id": "image_1"},
                 "source_refs": [{"source_file": "anonymous", "source_sheet": "brief", "source_row": "1"}]},
                {"shot_no": "2", "fields": {"image_id": "image_2"},
                 "source_refs": [{"source_file": "anonymous", "source_sheet": "brief", "source_row": "2"}]},
            ],
        )
        self.reviews.submit_draft(draft.draft_id, actor_id=owner["actor_id"],
                                  formal_version_id=self.version["version_id"])
        items = self.reviews.add_review_items(self.version["version_id"], items=[
            {"kind": "image", "shot_no": "1", "severity": "blocker",
             "evidence_ref": "artifact://review/one", "evidence_sha256": "1" * 64},
            {"kind": "image", "shot_no": "2", "severity": "blocker",
             "evidence_ref": "artifact://review/two", "evidence_sha256": "2" * 64},
        ])
        self.reviews.decide_review_items(
            self.version["version_id"], item_ids=[item.review_item_id for item in items],
            decision="approve", evidence_ref="artifact://review/approve",
            evidence_sha256="a" * 64, reviewer_actor_id=owner["actor_id"],
        )
        self.artifacts = FileVersionQualityArtifactStore(root / "quality-evidence")
        self.delivery = VersionDeliveryService(self.store, RunArtifactResolver(root / "runs"))
        self.resolver = self.new_resolver()

    def new_resolver(self):
        return build_workspace_version_quality_resolver(
            store=self.store, delivery_service=self.delivery,
            run_artifacts=RunArtifactResolver(self.root / "runs"), reviews=self.reviews,
            evidence_artifacts=self.artifacts,
        )

    @property
    def lock(self):
        return self.projects.get_project(self.token, self.project["project_id"])["project_version"]

    def confirm(self):
        return AuthenticatedFinalConfirmationService(
            resolver=self.resolver,
            actor_resolver=lambda *args, **kwargs: None,
        )


class VersionQualityPersistenceTests(unittest.TestCase):
    def test_migration_sequence_through_14_and_quality_guards_are_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp) / "workspace.sqlite3")
            store.initialize()
            with store.connection() as connection:
                migrations = connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
                triggers = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    )
                }
            self.assertEqual([row[0] for row in migrations], list(range(1, 28)))
            self.assertTrue({
                "version_quality_snapshot_scope_match",
                "version_quality_snapshot_link_scope_update",
                "version_quality_final_current_snapshot",
                "version_quality_snapshots_no_update",
                "version_quality_final_no_delete",
            }.issubset(triggers))

    def test_migration_9_persists_and_reopens_authoritative_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = QualityPersistenceFixture(Path(tmp))
            snapshot = fixture.resolver.materialize_shot_snapshot(
                fixture.token, project_id=fixture.project["project_id"],
                version_id=fixture.version["version_id"], expected_project_lock_version=fixture.lock,
            )
            command = AuthenticatedFinalConfirmationService(
                resolver=fixture.resolver, actor_resolver=_OwnerActorResolver(fixture.store)
            )
            final = command.confirm(
                fixture.token, project_id=fixture.project["project_id"],
                version_id=fixture.version["version_id"], expected_project_lock_version=fixture.lock,
                decision="quality_pass", evidence_ref="artifact://review/final",
                evidence_sha256="f" * 64,
            )
            reopened_store = WorkspaceStore(fixture.store.database_path)
            reopened = build_workspace_version_quality_resolver(
                store=reopened_store,
                delivery_service=VersionDeliveryService(reopened_store, RunArtifactResolver(fixture.root / "runs")),
                run_artifacts=RunArtifactResolver(fixture.root / "runs"), reviews=fixture.reviews,
                evidence_artifacts=fixture.artifacts,
            ).resolve_authoritative_evidence(
                fixture.token, project_id=fixture.project["project_id"],
                version_id=fixture.version["version_id"], expected_project_lock_version=fixture.lock,
            )
            self.assertEqual(reopened.shot_review.snapshot_ref, snapshot.snapshot_ref)
            self.assertEqual(reopened.final_confirmation.confirmation_id, final.confirmation_id)
            with reopened_store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_publications").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_quality_snapshots").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_quality_final_confirmations").fetchone()[0], 1)
                refs = connection.execute(
                    "SELECT snapshot_ref FROM version_quality_snapshots UNION ALL "
                    "SELECT confirmation_ref FROM version_quality_final_confirmations"
                ).fetchall()
            self.assertTrue(all(str(row[0]).startswith("artifact://") for row in refs))
            self.assertFalse((fixture.root / "CURRENT").exists())
            final_path = fixture.artifacts.root / "final-confirmation" / final.evidence_sha256[:2] / f"{final.evidence_sha256}.json"
            final_path.unlink()
            with self.assertRaisesRegex(WorkspaceServiceError, "NOT_FOUND"):
                reopened_store = WorkspaceStore(fixture.store.database_path)
                build_workspace_version_quality_resolver(
                    store=reopened_store,
                    delivery_service=VersionDeliveryService(reopened_store, RunArtifactResolver(fixture.root / "runs")),
                    run_artifacts=RunArtifactResolver(fixture.root / "runs"), reviews=fixture.reviews,
                    evidence_artifacts=fixture.artifacts,
                ).resolve_authoritative_evidence(
                    fixture.token, project_id=fixture.project["project_id"],
                    version_id=fixture.version["version_id"], expected_project_lock_version=fixture.lock,
                )

    def test_cross_project_lock_stale_tampered_and_second_final_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = QualityPersistenceFixture(Path(tmp))
            lock = fixture.lock
            fixture.resolver.materialize_shot_snapshot(
                fixture.token, project_id=fixture.project["project_id"],
                version_id=fixture.version["version_id"], expected_project_lock_version=lock,
            )
            repo = WorkspaceVersionQualityRepository(fixture.store)
            first = repo.current_snapshot(
                fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project["project_id"],
                version_id=fixture.version["version_id"], run_id=fixture.run_id,
                expected_project_lock_version=lock,
            )
            with fixture.store.transaction() as connection:
                with self.assertRaisesRegex(Exception, "IMMUTABLE_RECORD"):
                    connection.execute("UPDATE version_quality_snapshots SET total_count=9 WHERE snapshot_id=?", (first.snapshot_id,))
            changed = fixture.projects.rename_project(
                fixture.token, project_id=fixture.project["project_id"], name="匿名质量项目-锁漂移",
                expected_project_version=lock,
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
                fixture.resolver.resolve_authoritative_evidence(
                    fixture.token, project_id=fixture.project["project_id"],
                    version_id=fixture.version["version_id"], expected_project_lock_version=lock,
                )
            with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
                repo.current_snapshot(
                    fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project["project_id"],
                    version_id=fixture.version["version_id"], run_id=fixture.run_id,
                    expected_project_lock_version=lock,
                )
            self.assertEqual(changed["project_version"], lock + 1)

    def test_cross_project_and_duplicate_final_are_rejected_by_persistent_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = QualityPersistenceFixture(Path(tmp))
            lock = fixture.lock
            snapshot = fixture.resolver.materialize_shot_snapshot(
                fixture.token, project_id=fixture.project["project_id"],
                version_id=fixture.version["version_id"], expected_project_lock_version=lock,
            )
            command = AuthenticatedFinalConfirmationService(
                resolver=fixture.resolver, actor_resolver=_OwnerActorResolver(fixture.store)
            )
            command.confirm(
                fixture.token, project_id=fixture.project["project_id"],
                version_id=fixture.version["version_id"], expected_project_lock_version=lock,
                decision="quality_pass", evidence_ref="artifact://review/final",
                evidence_sha256="f" * 64,
            )
            repo = WorkspaceVersionQualityRepository(fixture.store)
            persisted = repo.current_snapshot(
                fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project["project_id"],
                version_id=fixture.version["version_id"], run_id=fixture.run_id,
                expected_project_lock_version=lock,
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "FINAL_CONFIRMATION_EXISTS"):
                repo.save_final(
                    fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project["project_id"],
                    version_id=fixture.version["version_id"], run_id=fixture.run_id,
                    snapshot_id=persisted.snapshot_id,
                    artifact=VersionQualityArtifact(
                        "artifact://version-quality/final-confirmation/" + "e" * 64, "e" * 64,
                    ),
                    decision="quality_pass",
                    reviewer_actor_id=_OwnerActorResolver(fixture.store).resolve_reviewer(
                        fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project["project_id"],
                    ),
                    confirmed_at="2026-08-13T00:00:00Z",
                    expected_project_lock_version=lock,
                )
            workspace = fixture.projects.get_workspace(fixture.token, fixture.workspace_id)
            other = fixture.projects.create_project(
                fixture.token, workspace_id=fixture.workspace_id, name="匿名其他项目",
                category_id="launch", expected_workspace_version=workspace["workspace_version"],
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "VERSION_NOT_CURRENT"):
                fixture.resolver.resolve_authoritative_evidence(
                    fixture.token, project_id=other["project_id"],
                    version_id=fixture.version["version_id"],
                    expected_project_lock_version=other["project_version"],
                )

    def test_database_rejects_cross_version_links_and_final_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = QualityPersistenceFixture(Path(tmp))
            lock = fixture.lock
            fixture.resolver.materialize_shot_snapshot(
                fixture.token, project_id=fixture.project["project_id"],
                version_id=fixture.version["version_id"], expected_project_lock_version=lock,
            )
            with fixture.store.transaction() as connection:
                snapshot_id = connection.execute(
                    "SELECT snapshot_id FROM version_quality_snapshot_links WHERE version_id=?",
                    (fixture.version["version_id"],),
                ).fetchone()[0]
                with self.assertRaisesRegex(sqlite3.IntegrityError, "QUALITY_SNAPSHOT_LINK_MISMATCH"):
                    connection.execute(
                        "UPDATE version_quality_snapshot_links SET snapshot_id=? WHERE version_id=?",
                        ("quality_snapshot_fake", fixture.version["version_id"]),
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "QUALITY_.*"):
                    connection.execute(
                        """INSERT INTO version_quality_final_confirmations VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?, 'quality_pass', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')""",
                        ("quality_final_fake", fixture.workspace_id, fixture.project["project_id"],
                         fixture.version["version_id"], fixture.run_id, "quality_snapshot_fake",
                         "artifact://version-quality/final-confirmation/" + "f" * 64, "f" * 64,
                         "actor_fake"),
                    )


class _OwnerActorResolver:
    def __init__(self, store):
        self.store = store

    def resolve_reviewer(self, auth_token, *, workspace_id, project_id):
        from mvp_v2.identity_service import require_permission
        with self.store.connection() as connection:
            return require_permission(
                connection, auth_token, workspace_id=workspace_id,
                project_id=project_id, permission="review.record",
            )["actor_id"]


if __name__ == "__main__":
    unittest.main()
