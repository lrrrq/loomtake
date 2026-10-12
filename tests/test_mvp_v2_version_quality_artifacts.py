from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.version_delivery import VersionDeliveryContext, VersionDeliveryService
from mvp_v2.version_quality import (
    AuthenticatedFinalConfirmationService,
    InMemoryFinalEvidenceLinkRepository,
    VersionQualityAccess,
    VersionQualityResolver,
    VersionDeliveryQualityAuthority,
)
from mvp_v2.version_quality_artifacts import FileVersionQualityArtifactStore
from mvp_v2.version_quality_artifacts import VersionQualityArtifact
from mvp_v2.version_review_service import (
    InMemoryVersionReviewRepository,
    VersionReviewService,
)
from mvp_v2.workspace_store import WorkspaceServiceError
from mvp_v2.workspace_store import WorkspaceStore


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AnonymousAuthority:
    def __init__(self, access: VersionQualityAccess):
        self.access = access
        self.calls: list[tuple[str, str, str]] = []

    def resolve(
        self, auth_token: str, *, project_id: str, version_id: str,
        expected_project_lock_version: int,
    ) -> VersionQualityAccess:
        self.calls.append((auth_token, project_id, version_id))
        if auth_token != "anonymous-authorized-token":
            raise WorkspaceServiceError("PERMISSION_DENIED", "匿名 fixture 拒绝未授权访问")
        if (project_id, version_id) != (self.access.project_id, self.access.version_id):
            raise WorkspaceServiceError("VERSION_NOT_CURRENT", "project/version 不匹配或不是当前版本")
        if expected_project_lock_version != self.access.project_lock_version:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "project lock 不匹配")
        return self.access


class AnonymousActorResolver:
    def resolve_reviewer(self, auth_token: str, *, workspace_id: str, project_id: str) -> str:
        if (
            auth_token != "anonymous-authorized-token"
            or workspace_id != "workspace_anonymous"
            or project_id != "project_anonymous"
        ):
            raise WorkspaceServiceError("PERMISSION_DENIED", "reviewer 不存在")
        return "actor_from_auth"

    def is_reviewer(self, actor_id: str, *, workspace_id: str, project_id: str) -> bool:
        return (
            actor_id == "actor_from_auth"
            and workspace_id == "workspace_anonymous"
            and project_id == "project_anonymous"
        )


class VersionQualityArtifactFixture:
    def __init__(self, root: Path):
        self.root = root
        self.run_id = "run_anon_0001"
        self.version_id = "version_anonymous_0001"
        self.project_id = "project_anonymous"
        self.workspace_id = "workspace_anonymous"
        run_dir = root / "runs" / "anonymous" / "runs" / self.run_id
        run_dir.mkdir(parents=True)
        self.report = run_dir / "run_report.json"
        self.report.write_text(json.dumps({"run_id": self.run_id, "machine_audit_status": "pass"}), encoding="utf-8")
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(json.dumps({
            "run_id": self.run_id,
            "artifacts": {"run_report": {"path": "run_report.json", "sha256": _file_sha(self.report)}},
        }, sort_keys=True), encoding="utf-8")
        self.access = VersionQualityAccess(
            workspace_id=self.workspace_id, project_id=self.project_id,
            project_lock_version=7, version_id=self.version_id, run_id=self.run_id,
            manifest_ref=f"artifact://run/{self.run_id}/manifest",
            manifest_sha256=_file_sha(manifest),
        )
        self.authority = AnonymousAuthority(self.access)
        self.reviews = VersionReviewService(InMemoryVersionReviewRepository())
        draft = self.reviews.create_draft(
            workspace_id=self.workspace_id, project_id=self.project_id,
            base_version_id="version_parent", actor_id="actor_editor",
            shots=[
                {"shot_no": "1", "fields": {"copy": "匿名开场", "image_id": "image_1"}, "source_refs": [{"source_file": "anonymous", "source_sheet": "brief", "source_row": "1"}]},
                {"shot_no": "2", "fields": {"copy": "匿名收束", "image_id": "image_2"}, "source_refs": [{"source_file": "anonymous", "source_sheet": "brief", "source_row": "2"}]},
            ],
        )
        self.reviews.submit_draft(
            draft.draft_id, actor_id="actor_editor", formal_version_id=self.version_id
        )
        self.store = FileVersionQualityArtifactStore(root / "evidence")
        self.links = InMemoryFinalEvidenceLinkRepository()
        self.resolver = self.new_resolver()

    def new_resolver(self) -> VersionQualityResolver:
        return VersionQualityResolver(
            authority=self.authority,
            run_artifacts=RunArtifactResolver(self.root / "runs"),
            reviews=self.reviews,
            evidence_artifacts=self.store,
            final_links=self.links,
            actor_verifier=AnonymousActorResolver(),
        )

    def approve_all_images(self) -> None:
        items = self.reviews.add_review_items(self.version_id, items=[
            {"kind": "image", "shot_no": "1", "severity": "blocker", "evidence_ref": "artifact://review/one", "evidence_sha256": _sha("one")},
            {"kind": "image", "shot_no": "2", "severity": "blocker", "evidence_ref": "artifact://review/two", "evidence_sha256": _sha("two")},
        ])
        self.reviews.decide_review_items(
            self.version_id, item_ids=[item.review_item_id for item in items], decision="approve",
            evidence_ref="artifact://review/approve", evidence_sha256=_sha("approve"),
            reviewer_actor_id="actor_reviewer",
        )

    def link_final_for_test(self, confirmation, snapshot: VersionQualityArtifact) -> None:
        artifact = self.store.write_json("final-confirmation", {
            "schema_version": "mplus-final-confirmation-evidence/v1",
            "workspace_id": self.workspace_id, "project_id": self.project_id,
            "version_id": self.version_id, "run_id": self.run_id,
            "total_count": 2, "snapshot_ref": snapshot.ref,
            "snapshot_sha256": snapshot.sha256,
            "confirmation": confirmation.__dict__.copy(),
        })
        self.links._finals[self.version_id] = artifact


class VersionQualityArtifactTests(unittest.TestCase):
    def test_c0_authority_adapter_reads_real_version_delivery_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkspaceStore(root / "workspace.sqlite3")
            identity = LocalIdentityAdapter(store)
            owner = identity.bootstrap_owner(
                workspace_name="匿名 C0", display_name="匿名管理员", confirmed=True,
                token="c0-owner-token",
            )
            projects = ProjectService(store)
            project = projects.create_project(
                owner["auth_token"], workspace_id=owner["workspace_id"],
                name="匿名 C0 项目", category_id="launch", expected_workspace_version=0,
            )
            run_id = "run_c0_0001"
            run_dir = root / "runs" / "anonymous" / "runs" / run_id
            run_dir.mkdir(parents=True)
            rows = [{"镜号": "1"}, {"镜号": "2"}]
            details = [{"模板镜号": "1"}, {"模板镜号": "2"}]
            stages = {}
            for stage, schema in (("script_plan", "mplus-script-plan/v2"), ("script_draft", "mplus-script-draft/v1"), ("visual_binding", "mplus-visual-binding/v1")):
                payload = {"schema_version": schema, "script_rows": rows, "detail_rows": details}
                if stage == "visual_binding":
                    payload["bindings"] = [{"shot_no": "1"}, {"shot_no": "2"}]
                path = run_dir / f"{stage}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                stages[stage] = path
            workbook = run_dir / "workbook.xlsx"
            workbook.write_bytes(b"anonymous workbook")
            report = run_dir / "run_report.json"
            report.write_text(json.dumps({"run_id": run_id, "machine_audit_status": "pass"}), encoding="utf-8")
            manifest = run_dir / "run_manifest.json"
            manifest.write_text(json.dumps({
                "run_id": run_id,
                "artifacts": {
                    "workbook": {"path": "workbook.xlsx", "sha256": _file_sha(workbook)},
                    "run_report": {"path": "run_report.json", "sha256": _file_sha(report)},
                },
                "stages": {name: {"stage_id": name, "status": "succeeded", "output_refs": [str(path.resolve())], "output_sha256": _file_sha(path)} for name, path in stages.items()},
            }, sort_keys=True), encoding="utf-8")
            bound = projects.bind_run(
                owner["auth_token"], project_id=project["project_id"], run_id=run_id,
                source_kind="native", manifest_ref=f"artifact://run/{run_id}/manifest",
                manifest_sha256=_file_sha(manifest), expected_project_version=0,
            )
            version = projects.create_project_version(
                owner["auth_token"], project_id=project["project_id"], run_id=run_id,
                content_sha256=_file_sha(workbook), artifact_ref=f"artifact://run/{run_id}/workbook",
                expected_project_version=bound["project_version"],
            )
            delivery = VersionDeliveryService(store, RunArtifactResolver(root / "runs"))
            context = delivery.resolve(owner["auth_token"], project["project_id"], version["version_id"])
            self.assertIsInstance(context, VersionDeliveryContext)
            access = VersionDeliveryQualityAuthority(delivery).resolve(
                owner["auth_token"], project_id=project["project_id"], version_id=version["version_id"],
                expected_project_lock_version=version["project_version"],
            )
            self.assertEqual((access.project_id, access.version_id, access.run_id), (project["project_id"], version["version_id"], run_id))
            reviews = VersionReviewService(InMemoryVersionReviewRepository())
            draft = reviews.create_draft(
                workspace_id=owner["workspace_id"], project_id=project["project_id"],
                base_version_id="version_parent", actor_id="actor_editor",
                shots=[
                    {"shot_no": "1", "fields": {"copy": "一"}, "source_refs": [{"source_file": "anonymous", "source_sheet": "brief", "source_row": "1"}]},
                    {"shot_no": "2", "fields": {"copy": "二"}, "source_refs": [{"source_file": "anonymous", "source_sheet": "brief", "source_row": "2"}]},
                ],
            )
            reviews.submit_draft(draft.draft_id, actor_id="actor_editor", formal_version_id=version["version_id"])
            links = InMemoryFinalEvidenceLinkRepository()
            resolver = VersionQualityResolver(
                authority=VersionDeliveryQualityAuthority(delivery),
                run_artifacts=RunArtifactResolver(root / "runs"), reviews=reviews,
                evidence_artifacts=FileVersionQualityArtifactStore(root / "evidence"),
                final_links=links, actor_verifier=AnonymousActorResolver(),
            )
            resolver.materialize_shot_snapshot(
                owner["auth_token"], project_id=project["project_id"],
                version_id=version["version_id"], expected_project_lock_version=version["project_version"],
            )
            evidence = resolver.resolve_authoritative_evidence(
                owner["auth_token"], project_id=project["project_id"],
                version_id=version["version_id"], expected_project_lock_version=version["project_version"],
            )
            self.assertEqual(
                (evidence.schema_version, evidence.workspace_id, evidence.project_id, evidence.version_id, evidence.run_id),
                ("mplus-version-quality-evidence/v1", owner["workspace_id"], project["project_id"], version["version_id"], run_id),
            )
            renamed = projects.rename_project(
                owner["auth_token"], project_id=project["project_id"], name="匿名 C0 项目改名",
                expected_project_version=version["project_version"],
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
                resolver.resolve_authoritative_evidence(
                    owner["auth_token"], project_id=project["project_id"],
                    version_id=version["version_id"], expected_project_lock_version=version["project_version"],
                )
            with store.transaction() as connection:
                connection.execute(
                    "UPDATE projects SET current_version_id = ? WHERE project_id = ?",
                    (None, project["project_id"]),
                )
            with self.assertRaisesRegex(WorkspaceServiceError, "VERSION_NOT_CURRENT"):
                resolver.resolve_authoritative_evidence(
                    owner["auth_token"], project_id=project["project_id"],
                    version_id=version["version_id"], expected_project_lock_version=renamed["project_version"],
                )

    def test_formal_resolver_writes_reads_and_reopens_bound_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionQualityArtifactFixture(Path(tmp))
            fixture.approve_all_images()
            fixture.resolver.materialize_shot_snapshot(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
            )
            before = fixture.resolver.resolve_authoritative_evidence(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
            )
            self.assertTrue(before.shot_review.snapshot_ref.startswith("artifact://version-quality/shot-review/"))
            self.assertEqual((before.shot_review.total_count, before.shot_review.resolved_count, before.shot_review.unresolved_count), (2, 2, 0))
            command = AuthenticatedFinalConfirmationService(
                resolver=fixture.resolver, actor_resolver=AnonymousActorResolver()
            )
            final = command.confirm(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
                decision="quality_pass",
                evidence_ref="artifact://review/final", evidence_sha256=_sha("final"),
            )
            self.assertEqual(final.reviewer_actor_id, "actor_from_auth")
            artifact = fixture.links.final("anonymous-authorized-token", fixture.access)
            self.assertIsNotNone(artifact)
            payload = fixture.store.read_json(artifact.ref, artifact.sha256)
            self.assertEqual(
                (payload["workspace_id"], payload["project_id"], payload["version_id"], payload["run_id"], payload["total_count"]),
                (fixture.workspace_id, fixture.project_id, fixture.version_id, fixture.run_id, 2),
            )
            self.assertEqual(payload["confirmation"]["reviewer_actor_id"], "actor_from_auth")
            reopened = fixture.new_resolver().resolve_authoritative_evidence(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
            )
            self.assertEqual(reopened.final_confirmation.confirmation_id, final.confirmation_id)
            self.assertTrue(reopened.shot_review.snapshot_ref.startswith("artifact://"))
            self.assertTrue(reopened.final_confirmation.evidence_ref.startswith("artifact://version-quality/final-confirmation/"))
            self.assertTrue(fixture.resolver.resolve_export_readiness(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
            ))
            self.assertFalse(hasattr(fixture.resolver, "resolve"))
            with self.assertRaises(TypeError):
                fixture.resolver.resolve_authoritative_evidence(
                    "anonymous-authorized-token", project_id=fixture.project_id,
                    version_id=fixture.version_id,
                )

    def test_rejects_tampering_cross_project_old_version_mapping_and_fake_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionQualityArtifactFixture(Path(tmp))
            fixture.approve_all_images()
            fixture.resolver.materialize_shot_snapshot(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
            )
            command = AuthenticatedFinalConfirmationService(
                resolver=fixture.resolver, actor_resolver=AnonymousActorResolver()
            )
            command.confirm(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
                decision="quality_pass",
                evidence_ref="artifact://review/final", evidence_sha256=_sha("final"),
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "VERSION_NOT_CURRENT"):
                fixture.resolver.resolve_authoritative_evidence("anonymous-authorized-token", project_id="project_other", version_id=fixture.version_id, expected_project_lock_version=7)
            with self.assertRaisesRegex(WorkspaceServiceError, "VERSION_NOT_CURRENT"):
                fixture.resolver.resolve_authoritative_evidence("anonymous-authorized-token", project_id=fixture.project_id, version_id="version_old", expected_project_lock_version=7)
            with self.assertRaises(TypeError):
                command.confirm(
                    "anonymous-authorized-token", project_id=fixture.project_id,
                    version_id=fixture.version_id, expected_project_lock_version=7,
                    decision="quality_pass",
                    evidence_ref="artifact://review/final", evidence_sha256=_sha("final"),
                    reviewer_actor_id="actor_forged",
                )
            with self.assertRaises(TypeError):
                fixture.resolver.resolve_authoritative_evidence(
                    "anonymous-authorized-token", project_id=fixture.project_id,
                    version_id=fixture.version_id, expected_project_lock_version=7, input_mapping={},
                )
            artifact = fixture.links.final("anonymous-authorized-token", fixture.access)
            digest = artifact.sha256
            path = fixture.store.root / "final-confirmation" / digest[:2] / f"{digest}.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(WorkspaceServiceError, "HASH_MISMATCH"):
                fixture.new_resolver().resolve_authoritative_evidence(
                    "anonymous-authorized-token", project_id=fixture.project_id,
                    version_id=fixture.version_id, expected_project_lock_version=7,
                )

    def test_rejects_run_report_and_shot_snapshot_byte_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionQualityArtifactFixture(Path(tmp))
            fixture.approve_all_images()
            fixture.resolver.materialize_shot_snapshot(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
            )
            evidence = fixture.resolver.resolve_authoritative_evidence(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
            )
            digest = evidence.shot_review.snapshot_sha256
            snapshot_path = fixture.store.root / "shot-review" / digest[:2] / f"{digest}.json"
            snapshot_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(WorkspaceServiceError, "HASH_MISMATCH"):
                fixture.new_resolver().resolve_authoritative_evidence(
                    "anonymous-authorized-token", project_id=fixture.project_id,
                    version_id=fixture.version_id, expected_project_lock_version=7,
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionQualityArtifactFixture(Path(tmp))
            fixture.resolver.materialize_shot_snapshot(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
            )
            fixture.report.write_text(
                json.dumps({"run_id": fixture.run_id, "machine_audit_status": "fail"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "ARTIFACT_HASH_MISMATCH"):
                fixture.resolver.resolve_authoritative_evidence(
                    "anonymous-authorized-token", project_id=fixture.project_id,
                    version_id=fixture.version_id, expected_project_lock_version=7,
                )

    def test_export_readiness_rejects_forged_actor_ref_old_link_and_deleted_final_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionQualityArtifactFixture(Path(tmp))
            fixture.approve_all_images()
            fixture.resolver.materialize_shot_snapshot(
                "anonymous-authorized-token", project_id=fixture.project_id, version_id=fixture.version_id, expected_project_lock_version=7,
            )
            confirmation = fixture.reviews.final_confirm(
                fixture.version_id, decision="quality_pass", evidence_ref="artifact://review/final",
                evidence_sha256=_sha("final"), reviewer_actor_id="actor_forged",
            )
            fixture.link_final_for_test(
                confirmation, fixture.links.snapshot("anonymous-authorized-token", fixture.access)
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
                fixture.resolver.resolve_export_readiness(
                    "anonymous-authorized-token", project_id=fixture.project_id, version_id=fixture.version_id, expected_project_lock_version=7,
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionQualityArtifactFixture(Path(tmp))
            old = fixture.resolver.materialize_shot_snapshot(
                "anonymous-authorized-token", project_id=fixture.project_id, version_id=fixture.version_id, expected_project_lock_version=7,
            )
            fixture.approve_all_images()
            fixture.resolver.materialize_shot_snapshot(
                "anonymous-authorized-token", project_id=fixture.project_id, version_id=fixture.version_id, expected_project_lock_version=7,
            )
            confirmation = fixture.reviews.final_confirm(
                fixture.version_id, decision="quality_pass", evidence_ref="artifact://review/final",
                evidence_sha256=_sha("final"), reviewer_actor_id="actor_from_auth",
            )
            fixture.link_final_for_test(confirmation, VersionQualityArtifact(old.snapshot_ref, old.snapshot_sha256))
            with self.assertRaisesRegex(WorkspaceServiceError, "最新快照"):
                fixture.resolver.resolve_export_readiness(
                    "anonymous-authorized-token", project_id=fixture.project_id, version_id=fixture.version_id, expected_project_lock_version=7,
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionQualityArtifactFixture(Path(tmp))
            fixture.approve_all_images()
            command = AuthenticatedFinalConfirmationService(
                resolver=fixture.resolver, actor_resolver=AnonymousActorResolver()
            )
            command.confirm(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
                decision="quality_pass",
                evidence_ref="artifact://review/final", evidence_sha256=_sha("final"),
            )
            fixture.links._finals[fixture.version_id] = VersionQualityArtifact(
                "artifact://version-quality/final-confirmation/" + "f" * 64,
                "f" * 64,
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "NOT_FOUND"):
                fixture.resolver.resolve_export_readiness(
                    "anonymous-authorized-token", project_id=fixture.project_id, version_id=fixture.version_id, expected_project_lock_version=7,
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionQualityArtifactFixture(Path(tmp))
            fixture.approve_all_images()
            command = AuthenticatedFinalConfirmationService(
                resolver=fixture.resolver, actor_resolver=AnonymousActorResolver()
            )
            command.confirm(
                "anonymous-authorized-token", project_id=fixture.project_id,
                version_id=fixture.version_id, expected_project_lock_version=7,
                decision="quality_pass",
                evidence_ref="artifact://review/final", evidence_sha256=_sha("final"),
            )
            final = fixture.links.final("anonymous-authorized-token", fixture.access)
            path = fixture.store.root / "final-confirmation" / final.sha256[:2] / f"{final.sha256}.json"
            path.unlink()
            with self.assertRaisesRegex(WorkspaceServiceError, "NOT_FOUND"):
                fixture.resolver.resolve_export_readiness(
                    "anonymous-authorized-token", project_id=fixture.project_id, version_id=fixture.version_id, expected_project_lock_version=7,
                )


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
