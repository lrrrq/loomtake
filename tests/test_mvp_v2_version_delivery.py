from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.version_delivery import VersionDeliveryContext, VersionDeliveryService
from mvp_v2.workspace_store import _MIGRATIONS, WorkspaceServiceError, WorkspaceStore


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VersionDeliveryFixture:
    def __init__(self, root: Path):
        self.root = root
        self.database_path = root / "runtime" / "workspace.sqlite3"
        self.runs_root = root / "runs"
        self.store = WorkspaceStore(self.database_path)
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="匿名管理员",
            confirmed=True,
            token="version-delivery-owner-token",
        )
        self.workspace_id = owner["workspace_id"]
        self.owner_token = owner["auth_token"]
        self.projects = ProjectService(self.store)
        self.delivery = VersionDeliveryService(
            self.store, RunArtifactResolver(self.runs_root)
        )
        self.run_index = 0

    def create_project(self, name: str) -> dict:
        workspace = self.projects.get_workspace(self.owner_token, self.workspace_id)
        return self.projects.create_project(
            self.owner_token,
            workspace_id=self.workspace_id,
            name=name,
            category_id="launch",
            expected_workspace_version=workspace["workspace_version"],
        )

    def bind_run(self, project: dict, *, payload_mutator=None) -> tuple[dict, Path]:
        self.run_index += 1
        run_id = f"run_anon_{self.run_index:04d}"
        run_dir = self.runs_root / "匿名项目" / "runs" / run_id
        run_dir.mkdir(parents=True)
        rows = [{"镜号": "1"}, {"镜号": "2"}]
        details = [
            {"模板镜号": "1", "source_refs": ["anonymous:1"]},
            {"模板镜号": "2", "source_refs": ["anonymous:2"]},
        ]
        stage_paths: dict[str, Path] = {}
        for stage, schema_version in (
            ("script_plan", "mplus-script-plan/v2"),
            ("script_draft", "mplus-script-draft/v1"),
            ("visual_binding", "mplus-visual-binding/v1"),
        ):
            payload = {
                "schema_version": schema_version,
                "script_rows": rows,
                "detail_rows": details,
            }
            if stage == "visual_binding":
                payload["bindings"] = [{"shot_no": "1"}, {"shot_no": "2"}]
            if payload_mutator is not None:
                payload_mutator(stage, payload)
            path = run_dir / f"{stage}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            stage_paths[stage] = path
        workbook = run_dir / "anonymous-workbook.xlsx"
        workbook.write_bytes(b"anonymous workbook")
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "artifacts": {
                        "workbook": {
                            "path": str(workbook.resolve()),
                            "sha256": _sha(workbook),
                        }
                    },
                    "stages": {
                        stage: {
                            "stage_id": stage,
                            "status": "succeeded",
                            "output_refs": [str(path.resolve())],
                            "output_sha256": _sha(path),
                        }
                        for stage, path in stage_paths.items()
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        bound = self.projects.bind_run(
            self.owner_token,
            project_id=project["project_id"],
            run_id=run_id,
            source_kind="native",
            manifest_ref=f"artifact://run/{run_id}/manifest",
            manifest_sha256=_sha(manifest),
            expected_project_version=self.projects.get_project(
                self.owner_token, project["project_id"]
            )["project_version"],
        )
        return bound, run_dir

    def create_version(self, project: dict, *, payload_mutator=None) -> tuple[dict, Path]:
        bound, run_dir = self.bind_run(project, payload_mutator=payload_mutator)
        workbook = run_dir / "anonymous-workbook.xlsx"
        version = self.projects.create_project_version(
            self.owner_token,
            project_id=project["project_id"],
            run_id=bound["run_id"],
            content_sha256=_sha(workbook),
            artifact_ref=f"artifact://run/{bound['run_id']}/workbook",
            expected_project_version=bound["project_version"],
        )
        return version, run_dir


class VersionDeliveryTests(unittest.TestCase):
    def test_explicit_ancestor_rejects_other_project_branch_and_stale_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionDeliveryFixture(Path(tmp))
            project = fixture.create_project("匿名祖先链")
            ancestor, _ = fixture.create_version(project)
            unrelated, _ = fixture.create_version(project)
            bound, run_dir = fixture.bind_run(project)
            child = fixture.projects.create_project_version(fixture.owner_token,
                project_id=project['project_id'], run_id=bound['run_id'],
                content_sha256=_sha(run_dir / 'anonymous-workbook.xlsx'),
                artifact_ref=f"artifact://run/{bound['run_id']}/workbook",
                parent_version_id=ancestor['version_id'],
                expected_project_version=bound['project_version'])
            context = fixture.delivery.resolve(fixture.owner_token, project['project_id'],
                ancestor['version_id'], ancestor_of=child['version_id'])
            self.assertEqual(context.version_id, ancestor['version_id'])
            other_project = fixture.create_project("匿名另一项目")
            foreign, _ = fixture.create_version(other_project)
            for target in (unrelated['version_id'], foreign['version_id']):
                with self.subTest(target=target), self.assertRaises(WorkspaceServiceError):
                    fixture.delivery.resolve(fixture.owner_token, project['project_id'],
                        target, ancestor_of=child['version_id'])
            with self.assertRaisesRegex(WorkspaceServiceError, 'VERSION_NOT_CURRENT'):
                fixture.delivery.resolve(fixture.owner_token, project['project_id'],
                    ancestor['version_id'], ancestor_of=unrelated['version_id'])

    def test_current_version_update_and_delivery_context_are_atomic_and_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionDeliveryFixture(Path(tmp))
            project = fixture.create_project("匿名版本项目")
            version, _ = fixture.create_version(project)

            stored = fixture.projects.get_project(
                fixture.owner_token, project["project_id"]
            )
            self.assertEqual(stored["current_version_id"], version["version_id"])
            context = fixture.delivery.resolve(
                fixture.owner_token,
                project["project_id"],
                version["version_id"],
                expected_project_version=version["project_version"],
            )

            self.assertIsInstance(context, VersionDeliveryContext)
            self.assertEqual(context.schema_version, "mplus-version-delivery-context/v1")
            self.assertEqual(
                set(context.as_dict()),
                {
                    "schema_version", "workspace_id", "project_id",
                    "project_lock_version", "version_id", "run_id",
                    "parent_version_id", "version_artifact", "manifest", "artifacts",
                },
            )
            serialized = json.dumps(context.as_dict(), ensure_ascii=False)
            self.assertNotIn(str(fixture.runs_root), serialized)
            self.assertNotIn("script_rows", serialized)
            self.assertEqual(
                set(context.as_dict()["artifacts"]),
                {"script_plan", "script_draft", "visual_binding"},
            )
            for artifact in (
                context.version_artifact,
                context.manifest,
                context.artifacts.script_plan,
                context.artifacts.script_draft,
                context.artifacts.visual_binding,
            ):
                self.assertTrue(artifact.ref.startswith("artifact://"))
                self.assertRegex(artifact.sha256, r"^[0-9a-f]{64}$")

    def test_version_creation_rolls_back_current_pointer_when_outbox_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionDeliveryFixture(Path(tmp))
            project = fixture.create_project("匿名回滚项目")
            bound, run_dir = fixture.bind_run(project)
            workbook = run_dir / "anonymous-workbook.xlsx"
            with patch("mvp_v2.project_service.enqueue_outbox", side_effect=RuntimeError("outbox fail")):
                with self.assertRaisesRegex(RuntimeError, "outbox fail"):
                    fixture.projects.create_project_version(
                        fixture.owner_token,
                        project_id=project["project_id"],
                        run_id=bound["run_id"],
                        content_sha256=_sha(workbook),
                        artifact_ref=f"artifact://run/{bound['run_id']}/workbook",
                        expected_project_version=bound["project_version"],
                    )
            stored = fixture.projects.get_project(
                fixture.owner_token, project["project_id"]
            )
            self.assertIsNone(stored["current_version_id"])
            self.assertEqual(fixture.projects.list_project_versions(
                fixture.owner_token, project["project_id"]
            ), [])

    def test_permissions_and_cross_project_current_pointer_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionDeliveryFixture(Path(tmp))
            allowed = fixture.create_project("允许读取")
            denied = fixture.create_project("禁止读取")
            allowed_version, _ = fixture.create_version(allowed)
            denied_version, _ = fixture.create_version(denied)
            workspace = fixture.projects.get_workspace(
                fixture.owner_token, fixture.workspace_id
            )
            viewer = fixture.identity.create_actor(
                fixture.owner_token,
                workspace_id=fixture.workspace_id,
                project_id=allowed["project_id"],
                display_name="匿名限定查看者",
                auth_source="local",
                role="viewer",
                expected_workspace_version=workspace["workspace_version"],
            )
            viewer_token = fixture.identity.issue_auth_session(
                actor_id=viewer["actor_id"], token="delivery-viewer-token"
            )["auth_token"]

            self.assertEqual(
                fixture.delivery.resolve(
                    viewer_token, allowed["project_id"], allowed_version["version_id"]
                ).version_id,
                allowed_version["version_id"],
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
                fixture.delivery.resolve(
                    viewer_token, denied["project_id"], denied_version["version_id"]
                )
            with fixture.store.transaction() as connection:
                connection.execute(
                    "UPDATE projects SET current_version_id = ? WHERE project_id = ?",
                    (denied_version["version_id"], allowed["project_id"]),
                )
            with self.assertRaisesRegex(WorkspaceServiceError, "INTEGRITY_ERROR"):
                fixture.delivery.resolve(
                    fixture.owner_token, allowed["project_id"], denied_version["version_id"]
                )

    def test_manifest_and_stage_sha_drift_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionDeliveryFixture(Path(tmp))
            project = fixture.create_project("匿名漂移项目")
            version, run_dir = fixture.create_version(project)
            (run_dir / "script_plan.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(WorkspaceServiceError, "ARTIFACT_HASH_MISMATCH"):
                fixture.delivery.resolve(
                    fixture.owner_token, project["project_id"], version["version_id"]
                )
            version, run_dir = fixture.create_version(project)
            (run_dir / "run_manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(WorkspaceServiceError, "ARTIFACT_HASH_MISMATCH"):
                fixture.delivery.resolve(
                    fixture.owner_token, project["project_id"], version["version_id"]
                )

    def test_stage_n_chain_rejects_detail_and_binding_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionDeliveryFixture(Path(tmp))
            project = fixture.create_project("匿名镜头链项目")
            version, _ = fixture.create_version(
                project,
                payload_mutator=lambda stage, payload: payload.update(
                    {"detail_rows": list(reversed(payload["detail_rows"]))}
                ) if stage == "script_draft" else None,
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "detail_rows 镜号链不一致"):
                fixture.delivery.resolve(
                    fixture.owner_token, project["project_id"], version["version_id"]
                )

            second = fixture.create_project("匿名绑定链项目")
            version, _ = fixture.create_version(
                second,
                payload_mutator=lambda stage, payload: payload.update(
                    {"bindings": [{"shot_no": "1"}, {"shot_no": "1"}]}
                ) if stage == "visual_binding" else None,
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "bindings 镜号链不一致|镜号缺失或重复"):
                fixture.delivery.resolve(
                    fixture.owner_token, second["project_id"], version["version_id"]
                )

    def test_second_database_check_rejects_current_and_lock_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionDeliveryFixture(Path(tmp))
            project = fixture.create_project("匿名二次核验项目")
            first, _ = fixture.create_version(project)
            second, _ = fixture.create_version(project)
            with fixture.store.transaction() as connection:
                connection.execute(
                    "UPDATE projects SET current_version_id = ? WHERE project_id = ?",
                    (first["version_id"], project["project_id"]),
                )
            original_validate = fixture.delivery._validate_stage_chain

            def change_current(resolved):
                with fixture.store.transaction() as connection:
                    connection.execute(
                        "UPDATE projects SET current_version_id = ? WHERE project_id = ?",
                        (second["version_id"], project["project_id"]),
                    )
                original_validate(resolved)

            with patch.object(fixture.delivery, "_validate_stage_chain", side_effect=change_current):
                with self.assertRaisesRegex(WorkspaceServiceError, "VERSION_NOT_CURRENT"):
                    fixture.delivery.resolve(
                        fixture.owner_token, project["project_id"], first["version_id"]
                    )

            def change_lock(resolved):
                with fixture.store.transaction() as connection:
                    connection.execute(
                        "UPDATE projects SET lock_version = lock_version + 1 WHERE project_id = ?",
                        (project["project_id"],),
                    )
                original_validate(resolved)

            with patch.object(fixture.delivery, "_validate_stage_chain", side_effect=change_lock):
                with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
                    fixture.delivery.resolve(
                        fixture.owner_token, project["project_id"], second["version_id"]
                    )

    def test_second_database_check_rejects_revoked_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionDeliveryFixture(Path(tmp))
            project = fixture.create_project("匿名会话复核项目")
            version, _ = fixture.create_version(project)
            original_validate = fixture.delivery._validate_stage_chain

            def revoke_session(resolved):
                with fixture.store.transaction() as connection:
                    connection.execute(
                        "UPDATE auth_sessions SET revoked_at = '2026-08-13T00:00:00Z' WHERE token_hash = ?",
                        (hashlib.sha256(fixture.owner_token.encode("utf-8")).hexdigest(),),
                    )
                original_validate(resolved)

            with patch.object(fixture.delivery, "_validate_stage_chain", side_effect=revoke_session):
                with self.assertRaisesRegex(WorkspaceServiceError, "AUTH_SESSION_INVALID"):
                    fixture.delivery.resolve(
                        fixture.owner_token, project["project_id"], version["version_id"]
                    )

    def test_restart_recovery_reads_the_same_current_version_without_publication_or_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = VersionDeliveryFixture(Path(tmp))
            project = fixture.create_project("匿名恢复项目")
            version, _ = fixture.create_version(project)
            restarted = VersionDeliveryService(
                WorkspaceStore(fixture.database_path), RunArtifactResolver(fixture.runs_root)
            )
            context = restarted.resolve(
                fixture.owner_token, project["project_id"], version["version_id"]
            )
            self.assertEqual(context.version_id, version["version_id"])
            with fixture.store.connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM version_publications").fetchone()[0],
                    0,
                )
            self.assertFalse((fixture.root / "CURRENT").exists())

    def test_additive_migration_leaves_preexisting_project_current_version_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "workspace.sqlite3"
            legacy = WorkspaceStore(database_path)
            with legacy.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                for version, name, statements in _MIGRATIONS[:6]:
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations VALUES (?, ?, '2026-08-13T00:00:00Z')",
                        (version, name),
                    )
                WorkspaceStore._seed_permissions(connection)
                connection.execute(
                    "INSERT INTO workspaces VALUES ('workspace_legacy', '匿名旧工作区', 0, '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')"
                )
                connection.execute(
                    "INSERT INTO actors VALUES ('actor_legacy', '匿名旧管理员', 'local', 'active', '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')"
                )
                connection.execute(
                    "INSERT INTO projects(project_id, workspace_id, name, category_id, status, lock_version, initial_brief_snapshot_id, current_brief_snapshot_id, created_by_actor_id, created_at, updated_at) VALUES ('project_legacy', 'workspace_legacy', '匿名旧项目', 'launch', 'active', 0, NULL, NULL, 'actor_legacy', '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')"
                )
                connection.commit()

            legacy.initialize()
            with legacy.connection() as connection:
                current = connection.execute(
                    "SELECT current_version_id FROM projects WHERE project_id = 'project_legacy'"
                ).fetchone()[0]
                migration = connection.execute(
                    "SELECT name FROM schema_migrations WHERE version = 7"
                ).fetchone()[0]
            self.assertIsNone(current)
            self.assertEqual(migration, "project-current-version")


if __name__ == "__main__":
    unittest.main()
