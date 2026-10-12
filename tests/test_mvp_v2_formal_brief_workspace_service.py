from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp_v2.formal_brief_domain import ArtifactPayload, FormalBriefDomainError, SourceManifest
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


class FormalBriefWorkspaceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-r01-formal-")
        self.root = Path(self.temporary.name)
        self.store = WorkspaceStore(self.root / "runtime" / "workspace.sqlite3")
        identity = LocalIdentityAdapter(self.store)
        owner = identity.bootstrap_owner(workspace_name="匿名工作区", display_name="匿名 owner", confirmed=True, token="anonymous-owner-token")
        self.token, self.workspace_id = owner["auth_token"], owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.project_a = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名项目甲", category_id="发布会",
            expected_workspace_version=0, brief_ref="artifact://brief/" + "a" * 64, brief_sha256="a" * 64,
        )
        self.project_b = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名项目乙", category_id="发布会",
            expected_workspace_version=1, brief_ref="artifact://brief/" + "b" * 64, brief_sha256="b" * 64,
        )
        self.service = FormalBriefWorkspaceService(self.store, artifact_root=self.root / "runtime")

    def tearDown(self):
        self.temporary.cleanup()

    def test_same_store_confirmation_restart_legacy_compatibility_and_immutability(self):
        manifest, artifacts, extracted = _conflicted_input(self.project_a["project_id"])
        pending = self.service.import_frozen(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            source_manifest=manifest, source_artifacts=artifacts, extracted=extracted, expected_project_version=0,
        )
        self.assertEqual(self.projects.get_current_brief_snapshot(self.token, self.project_a["project_id"])["brief_snapshot_id"], self.project_a["initial_brief_snapshot_id"])
        with self.assertRaisesRegex(FormalBriefDomainError, "不可确认"):
            self.service.confirm(
                self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
                brief_version_id=pending.brief_version_id, confirmed_at="2026-08-13T00:00:00Z", expected_project_version=1,
            )
        conflict = pending.payload_data["conflicts"][0]
        resolved = self.service.resolve_conflict(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            brief_version_id=pending.brief_version_id,
            conflict_id=conflict["conflict_id"], selected_fact_id=conflict["fact_ids"][0], expected_project_version=1,
        )
        resolution = resolved.payload_data["conflicts"][0]["resolution_evidence"]
        self.assertTrue(resolution["ref"].startswith("artifact://formal-brief/"))
        self.assertEqual(len(resolution["sha256"]), 64)
        self.assertTrue(resolved.payload_data["conflicts"][0]["resolved_by_actor_id"])
        self.assertRegex(resolved.payload_data["conflicts"][0]["resolved_at"], r"Z$")
        confirmed = self.service.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            brief_version_id=resolved.brief_version_id, confirmed_at="2026-08-13T00:00:01Z", expected_project_version=2,
        )
        payload = self.service.resolve_confirmed(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"], confirmed=confirmed,
        )
        self.assertEqual(payload["project_id"], self.project_a["project_id"])
        self.assertNotEqual(confirmed.brief_snapshot_id, self.project_a["initial_brief_snapshot_id"])
        restarted = FormalBriefWorkspaceService(WorkspaceStore(self.store.database_path), artifact_root=self.root / "runtime")
        self.assertEqual(
            restarted.resolve_confirmed(self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"], confirmed=confirmed)["project_id"],
            self.project_a["project_id"],
        )
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT name FROM schema_migrations WHERE version=8").fetchone()["name"], "t01_formal_brief_v2")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                connection.execute("UPDATE brief_versions SET created_at=created_at WHERE brief_version_id=?", (resolved.brief_version_id,))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                connection.execute("DELETE FROM brief_confirmations WHERE confirmation_id=?", (confirmed.confirmation_id,))

    def test_cross_project_snapshot_binding_and_lock_conflict_are_rejected(self):
        manifest, artifacts, extracted = _conflicted_input(self.project_a["project_id"])
        with self.assertRaisesRegex(WorkspaceServiceError, "AUTH_SESSION_INVALID"):
            self.service.import_frozen(
                "invalid-token", workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
                source_manifest=manifest, source_artifacts=artifacts, extracted=extracted, expected_project_version=0,
            )
        pending = self.service.import_frozen(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            source_manifest=manifest, source_artifacts=artifacts, extracted=extracted, expected_project_version=0,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.service.import_frozen(
                self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
                source_manifest=manifest, source_artifacts=artifacts, extracted=extracted, expected_project_version=0,
            )
        with self.store.transaction() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "CONFIRMATION_BINDING_MISMATCH"):
                connection.execute(
                    """INSERT INTO brief_confirmations(
                        confirmation_id, brief_version_id, brief_snapshot_id, workspace_id, project_id,
                        confirmation_status, unresolved_conflict_count, confirmed_by_actor_id, confirmed_at
                    ) VALUES (?, ?, ?, ?, ?, 'confirmed', 0, ?, ?)""",
                    ("forged_confirmation", pending.brief_version_id, self.project_b["initial_brief_snapshot_id"],
                     self.workspace_id, self.project_a["project_id"], "actor_forged", "2026-08-13T00:00:00Z"),
                )
        conflict = pending.payload_data["conflicts"][0]
        resolved = self.service.resolve_conflict(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            brief_version_id=pending.brief_version_id,
            conflict_id=conflict["conflict_id"], selected_fact_id=conflict["fact_ids"][0], expected_project_version=1,
        )
        confirmed = self.service.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            brief_version_id=resolved.brief_version_id, confirmed_at="2026-08-13T00:00:01Z", expected_project_version=2,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
            self.service.resolve_confirmed(
                self.token, workspace_id=self.workspace_id, project_id=self.project_b["project_id"], confirmed=confirmed,
            )

    def test_failed_resolution_rolls_back_database_without_deleting_shared_artifacts(self):
        manifest, artifacts, extracted = _conflicted_input(self.project_a["project_id"])
        pending = self.service.import_frozen(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            source_manifest=manifest, source_artifacts=artifacts, extracted=extracted, expected_project_version=0,
        )
        artifact_root = self.root / "runtime" / "formal-brief-payloads"
        before = sorted(path.relative_to(artifact_root) for path in artifact_root.rglob("*") if path.is_file())
        original_bytes = {path: (artifact_root / path).read_bytes() for path in before}
        with self.store.connection() as connection:
            database_before = list(connection.iterdump())
        conflict = pending.payload_data["conflicts"][0]
        with patch("mvp_v2.formal_brief_workspace_service._persist_derived_version", side_effect=RuntimeError("forced-db-failure")):
            with self.assertRaisesRegex(RuntimeError, "forced-db-failure"):
                self.service.resolve_conflict(
                    self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
                    brief_version_id=pending.brief_version_id, conflict_id=conflict["conflict_id"],
                    selected_fact_id=conflict["fact_ids"][0], expected_project_version=1,
                )
        after = sorted(path.relative_to(artifact_root) for path in artifact_root.rglob("*") if path.is_file())
        self.assertTrue(set(before) <= set(after))
        for path, original in original_bytes.items():
            self.assertEqual((artifact_root / path).read_bytes(), original)
        # Published content-addressed files may already be adopted elsewhere;
        # RV06-01 retains them, but the failed transaction must publish no rows.
        with self.store.connection() as connection:
            self.assertEqual(list(connection.iterdump()), database_before)

    def test_multiple_conflicts_resolve_as_successive_rekeyed_versions(self):
        manifest, artifacts, extracted = _multi_conflicted_input(self.project_a["project_id"])
        pending = self.service.import_frozen(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            source_manifest=manifest, source_artifacts=artifacts, extracted=extracted,
            expected_project_version=0,
        )
        first = pending.payload_data["conflicts"][0]
        after_first = self.service.resolve_conflict(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            brief_version_id=pending.brief_version_id, conflict_id=first["conflict_id"],
            selected_fact_id=first["fact_ids"][0], expected_project_version=1,
        )
        remaining = next(item for item in after_first.payload_data["conflicts"] if item["resolution_status"] == "unresolved")
        after_second = self.service.resolve_conflict(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            brief_version_id=after_first.brief_version_id, conflict_id=remaining["conflict_id"],
            selected_fact_id=remaining["fact_ids"][0], expected_project_version=2,
        )
        self.assertEqual(after_first.parent_brief_version_id, pending.brief_version_id)
        self.assertEqual(after_second.parent_brief_version_id, after_first.brief_version_id)
        self.assertTrue(all(item["resolution_status"] == "resolved" for item in after_second.payload_data["conflicts"]))
        confirmed = self.service.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            brief_version_id=after_second.brief_version_id, confirmed_at="2026-08-14T00:00:00Z",
            expected_project_version=3,
        )
        self.assertEqual(
            self.service.resolve_confirmed(
                self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"], confirmed=confirmed,
            )["project_id"],
            self.project_a["project_id"],
        )

    def test_resolution_distinguishes_candidates_with_identical_evidence_refs(self):
        manifest, artifacts, extracted = _conflicted_input(self.project_a["project_id"])
        extracted["facts"][1]["evidence_refs"] = list(extracted["facts"][0]["evidence_refs"])
        pending = self.service.import_frozen(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            source_manifest=manifest, source_artifacts=artifacts, extracted=extracted,
            expected_project_version=0,
        )
        conflict = pending.payload_data["conflicts"][0]
        resolved = self.service.resolve_conflict(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            brief_version_id=pending.brief_version_id, conflict_id=conflict["conflict_id"],
            selected_fact_id=conflict["fact_ids"][1], expected_project_version=1,
        )
        resolution = resolved.payload_data["conflicts"][0]["resolution_evidence"]
        resolution_payload = self.service.artifacts.read(
            ArtifactPayload.from_mapping(resolution)
        ).decode("utf-8")
        self.assertIn('"schema_version":"mplus-formal-resolution-evidence/v2"', resolution_payload)
        self.assertIn('"selected_fact_value_sha256"', resolution_payload)
        confirmed = self.service.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            brief_version_id=resolved.brief_version_id, confirmed_at="2026-08-14T00:00:00Z",
            expected_project_version=2,
        )
        payload = self.service.resolve_confirmed(
            self.token, workspace_id=self.workspace_id, project_id=self.project_a["project_id"],
            confirmed=confirmed,
        )
        resolved_conflict = payload["conflicts"][0]
        selected_fact = next(
            item for item in payload["facts"]
            if item["fact_id"] == resolved_conflict["resolved_fact_id"]
        )
        self.assertEqual(selected_fact["value_json"], '"匿名乙"')


def _conflicted_input(project_id: str):
    digest = "c" * 64
    source_id = "anonymous_doc"
    manifest = SourceManifest.from_mapping({
        "manifest": {"ref": "artifact://manifest/" + digest, "sha256": digest},
        "documents": [{"source_document_id": source_id, "relative_path": "inputs/anonymous.txt", "content_sha256": digest, "critical": True}],
    })
    source_artifacts = {source_id: ArtifactPayload("artifact://source/" + digest, digest)}
    extracted = {
        "project_id": project_id,
        "source_documents": [{"document_id": source_id, "project_id": project_id, "filename": "anonymous.txt", "mime_type": "text/plain", "size_bytes": 24, "sha256": digest, "critical": True}],
        "extractions": [{"document_id": source_id, "status": "succeeded", "critical": True, "error": None, "locations": [
            {"evidence_ref": "artifact://evidence/anonymous/page/1", "location": {"page": 1}, "text": "项目名称: 匿名甲"},
            {"evidence_ref": "artifact://evidence/anonymous/page/2", "location": {"page": 2}, "text": "项目名称: 匿名乙"},
        ]}],
        "images": [],
        "facts": [
            {"fact_id": "fact_a", "field": "项目名称", "value": "匿名甲", "confidence": "direct", "evidence_refs": ["artifact://evidence/anonymous/page/1"]},
            {"fact_id": "fact_b", "field": "项目名称", "value": "匿名乙", "confidence": "direct", "evidence_refs": ["artifact://evidence/anonymous/page/2"]},
            {"fact_id": "fact_tone", "field": "风格调性", "value": "克制可信", "confidence": "direct", "evidence_refs": ["artifact://evidence/anonymous/page/1"]},
            {"fact_id": "fact_goal", "field": "视频目标", "value": "说明正式发布重点", "confidence": "direct", "evidence_refs": ["artifact://evidence/anonymous/page/1"]},
            {"fact_id": "fact_avoid", "field": "禁止或不能出现的内容", "value": "避免无依据夸张", "confidence": "direct", "evidence_refs": ["artifact://evidence/anonymous/page/2"]},
        ],
        "conflicts": [{"conflict_id": "conflict_project", "field": "项目名称", "fact_ids": ["fact_a", "fact_b"], "status": "unresolved"}],
    }
    return manifest, source_artifacts, extracted


def _multi_conflicted_input(project_id: str):
    manifest, source_artifacts, extracted = _conflicted_input(project_id)
    locations = extracted["extractions"][0]["locations"]
    locations.extend([
        {"evidence_ref": "artifact://evidence/anonymous/page/3", "location": {"page": 3}, "text": "视频目标: 匿名目标甲"},
        {"evidence_ref": "artifact://evidence/anonymous/page/4", "location": {"page": 4}, "text": "视频目标: 匿名目标乙"},
    ])
    extracted["facts"].extend([
        {"fact_id": "fact_goal_a", "field": "视频目标", "value": "匿名目标甲", "confidence": "direct", "evidence_refs": ["artifact://evidence/anonymous/page/3"]},
        {"fact_id": "fact_goal_b", "field": "视频目标", "value": "匿名目标乙", "confidence": "direct", "evidence_refs": ["artifact://evidence/anonymous/page/4"]},
    ])
    extracted["conflicts"].append({"conflict_id": "conflict_goal", "field": "视频目标", "fact_ids": ["fact_goal_a", "fact_goal_b"], "status": "unresolved"})
    return manifest, source_artifacts, extracted
