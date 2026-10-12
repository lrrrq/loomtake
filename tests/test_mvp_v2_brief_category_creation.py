from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp_v2 import workspace_store as store_module
from mvp_v2.formal_brief_domain import FormalBriefDomainError
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore
from tests.test_mvp_v2_formal_brief_input_adapter import _upload


_CATEGORIES = (
    ("发布会", "launch"), ("展会", "exhibition"), ("年会", "annual"),
    ("体育赛事", "sports"), ("音乐节", "music_festival"), ("明星活动", "star_activity"),
)


def _fixture(root):
    store = WorkspaceStore(root / "workspace.sqlite3")
    owner = LocalIdentityAdapter(store).bootstrap_owner(
        workspace_name="匿名分类工作区", display_name="匿名管理员",
        confirmed=True, token="anonymous-category-owner",
    )
    projects = ProjectService(store)
    adapter = FormalBriefInputAdapter(FormalBriefWorkspaceService(store, artifact_root=root), data_root=root)
    return store, owner, projects, adapter


def _brief(script_type=None, *, specific_type=None):
    lines = [
        "项目名称：匿名分类验收", "目标时长：60s", "风格调性：克制真实",
        "视频目标：记录活动流程", "必须出现：品牌标识与活动主要环节",
        "禁止：无依据夸张",
    ]
    if script_type is not None:
        lines.append(f"脚本类型：{script_type}")
    if specific_type is not None:
        lines.append(f"具体类型：{specific_type}")
    return "\n".join(lines)


def _database_rows(store):
    with store.connection() as connection:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        )]
        return {
            name: sorted((tuple(row) for row in connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"')), key=repr)
            for name in names
        }


class BriefCategoryCreationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-brief-category-")
        self.root = Path(self.temporary.name)
        self.store, self.owner, self.projects, self.adapter = _fixture(self.root / "current")
        self.token, self.workspace_id = self.owner["auth_token"], self.owner["workspace_id"]

    def tearDown(self):
        self.temporary.cleanup()

    def _new_project(self, category=None):
        return self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名待分类项目", category_id=category,
            expected_workspace_version=self.projects.get_workspace(self.token, self.workspace_id)["workspace_version"],
        )

    def _import(self, project, text):
        return self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=project["project_id"],
            uploads=[_upload("匿名分类需求.txt", text)], expected_project_version=project["project_version"],
        )

    def _project(self, project_id):
        with self.store.connection() as connection:
            return dict(connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone())

    def _confirm(self, project, brief, expected_version=1):
        return self.adapter.confirm(
            self.token, workspace_id=self.workspace_id, project_id=project["project_id"],
            brief_version_id=brief["brief_key"], confirmed_at="2026-09-08T00:00:00Z",
            expected_project_version=expected_version,
        )

    def test_schema21_projects_and_foreign_key_rows_survive_nullable_migration(self):
        legacy_root = self.root / "legacy"
        legacy_migrations = tuple(item for item in store_module._MIGRATIONS if item[0] <= 21)
        with patch.object(store_module, "_MIGRATIONS", legacy_migrations):
            store, owner, projects, adapter = _fixture(legacy_root)
            token, workspace_id = owner["auth_token"], owner["workspace_id"]
            project = projects.create_project(
                token, workspace_id=workspace_id, name="匿名历史项目", category_id="launch",
                expected_workspace_version=0, brief_ref="artifact://brief/" + "a" * 64, brief_sha256="a" * 64,
            )
            projects.create_project(token, workspace_id=workspace_id, name="匿名关联项目", category_id="music_festival", expected_workspace_version=1)
            brief = adapter.import_uploads(
                token, workspace_id=workspace_id, project_id=project["project_id"],
                uploads=[_upload("历史需求.txt", _brief("发布会"))], expected_project_version=0,
            )
            adapter.confirm(token, workspace_id=workspace_id, project_id=project["project_id"],
                brief_version_id=brief["brief_key"], confirmed_at="2026-09-08T00:00:00Z", expected_project_version=1)
            run = projects.bind_run(token, project_id=project["project_id"], run_id="anonymous-category-run",
                source_kind="native", manifest_ref="artifact://run/anonymous-category-run/manifest",
                manifest_sha256="b" * 64, expected_project_version=2)
            projects.create_project_version(token, project_id=project["project_id"], run_id=run["run_id"],
                content_sha256="c" * 64, artifact_ref="artifact://run/anonymous-category-run/reviewable",
                expected_project_version=run["project_version"])
            with store.connection() as connection:
                self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 21)
                category = next(row for row in connection.execute("PRAGMA table_info(projects)") if row[1] == "category_id")
                self.assertEqual(category[3], 1)
                before_objects = [tuple(row) for row in connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE tbl_name='projects' AND type IN ('index','trigger') ORDER BY name"
                )]
            before = _database_rows(store)

        store.initialize()
        expected_after = {**before, "formal_image_progress": [], "formal_image_progress_requests": [],
                          "creative_draft_heads": [], "project_image_generated_sources": [],
                          "manual_command_results": []}
        self.assertEqual(_database_rows(store), expected_after)
        with store.connection() as connection:
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 27)
            category = next(row for row in connection.execute("PRAGMA table_info(projects)") if row[1] == "category_id")
            self.assertEqual(category[3], 0)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual([tuple(row) for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE tbl_name='projects' AND type IN ('index','trigger') ORDER BY name"
            )], before_objects)
            for table in ("project_runs", "project_versions", "project_brief_snapshots", "brief_versions"):
                self.assertIn("projects", {row[2] for row in connection.execute(f"PRAGMA foreign_key_list({table})")})
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM projects WHERE project_id=?", (project["project_id"],))
        store.initialize()
        self.assertEqual(_database_rows(store), expected_after)
        created = projects.create_project(token, workspace_id=workspace_id, name="匿名未分类项目", expected_workspace_version=2)
        self.assertIsNone(created["category_id"])

    def test_unclassified_project_is_bound_only_when_each_of_six_briefs_is_confirmed(self):
        for label, category_id in _CATEGORIES:
            with self.subTest(category_id=category_id):
                project = self._new_project()
                self.assertIsNone(project["category_id"])
                pending = self._import(project, _brief(label))
                self.assertEqual(pending["routing"]["status"], "ready")
                self.assertEqual(pending["routing"]["category_id"], category_id)
                self.assertIsNone(self._project(project["project_id"])["category_id"])
                confirmed = self._confirm(project, pending)
                self.assertEqual(confirmed["confirmation_status"], "confirmed")
                self.assertEqual(confirmed["brief"]["facts"], pending["brief"]["facts"])
                current = self._project(project["project_id"])
                self.assertEqual(current["category_id"], category_id)
                self.assertEqual(current["lock_version"], 2)
                self.assertTrue(current["current_brief_snapshot_id"])
                self.assertTrue(current["current_creative_standard_version_id"])
                restarted = FormalBriefInputAdapter(FormalBriefWorkspaceService(WorkspaceStore(self.store.database_path), artifact_root=self.root / "current"), data_root=self.root / "current")
                again = restarted.read_current_formal(self.token, workspace_id=self.workspace_id, project_id=project["project_id"])
                self.assertEqual(again["confirmation_status"], "confirmed")
                self.assertEqual(again["routing"]["category_id"], category_id)

    def test_unknown_or_cross_field_category_conflict_stays_pending_without_confirmation_writes(self):
        for text, status in ((_brief("匿名纪录片"), "unsupported"), (_brief("发布会", specific_type="音乐节"), "needs_clarification")):
            with self.subTest(status=status):
                project = self._new_project()
                pending = self._import(project, text)
                self.assertEqual(pending["routing"]["status"], status)
                before = _database_rows(self.store)
                with self.assertRaisesRegex(WorkspaceServiceError, "BRIEF_CATEGORY_REQUIRED"):
                    self._confirm(project, pending)
                self.assertEqual(_database_rows(self.store), before)
                current = self.adapter.read_current_formal(self.token, workspace_id=self.workspace_id, project_id=project["project_id"])
                self.assertEqual(current["confirmation_status"], "pending")
                self.assertEqual(current["routing"]["status"], status)
                self.assertIsNone(self._project(project["project_id"])["category_id"])

    def test_category_fact_conflict_uses_existing_brief_choice_then_binds_selected_fact(self):
        project = self._new_project()
        pending = self._import(project, _brief("发布会") + "\n脚本类型：音乐节")
        self.assertEqual(pending["unresolved_conflict_count"], 1)
        before = _database_rows(self.store)
        with self.assertRaises(FormalBriefDomainError):
            self._confirm(project, pending)
        self.assertEqual(_database_rows(self.store), before)
        conflict = pending["brief"]["conflicts"][0]
        chosen = next(item for item in conflict["choices"] if json.loads(item["value"]) == "音乐节")
        resolved = self.adapter.resolve_conflict(self.token, workspace_id=self.workspace_id,
            project_id=project["project_id"], brief_version_id=pending["brief_key"],
            conflict_id=conflict["conflict_key"], selected_fact_id=chosen["fact_key"], expected_project_version=1)
        self.assertEqual(resolved["routing"]["category_id"], "music_festival")
        self.assertIsNone(self._project(project["project_id"])["category_id"])
        self._confirm(project, resolved, expected_version=2)
        self.assertEqual(self._project(project["project_id"])["category_id"], "music_festival")

    def test_legacy_explicit_category_without_category_fact_remains_compatible(self):
        project = self._new_project("launch")
        pending = self._import(project, _brief())
        self.assertEqual(self._confirm(project, pending)["confirmation_status"], "confirmed")
        self.assertEqual(self._project(project["project_id"])["category_id"], "launch")

    def test_confirm_clears_only_a_changed_category_binding_and_obeys_project_lock(self):
        for label, expected_ref in (("发布会", "anonymous-launch@v1"), ("音乐节", None)):
            with self.subTest(label=label):
                project = self._new_project("launch")
                # Persisted anonymous binding: this test owns category confirmation,
                # not package selection or package identity validation.
                with self.store.transaction() as connection:
                    connection.execute("UPDATE projects SET template_package_ref=?, template_package_sha256=? WHERE project_id=?",
                        ("anonymous-launch@v1", "d" * 64, project["project_id"]))
                pending = self._import(project, _brief(label))
                before = _database_rows(self.store)
                with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
                    self._confirm(project, pending, expected_version=0)
                self.assertEqual(_database_rows(self.store), before)
                self._confirm(project, pending)
                current = self._project(project["project_id"])
                self.assertEqual(current["template_package_ref"], expected_ref)
                self.assertEqual(current["template_package_sha256"], "d" * 64 if expected_ref else None)


if __name__ == "__main__":
    unittest.main()
