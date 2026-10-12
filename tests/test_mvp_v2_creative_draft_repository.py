from pathlib import Path
import tempfile
import unittest

from mvp_v2.creative_draft_repository import CreativeDraftRepository
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.version_review_service import VersionReviewService, VersionReviewError
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


class CreativeDraftRepositoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = WorkspaceStore(self.root / "workspace.sqlite3")
        owner = LocalIdentityAdapter(self.store).bootstrap_owner(workspace_name="匿名工作区",
            display_name="匿名作者", confirmed=True, token="creative-fixture-owner")
        self.token, self.workspace_id = owner["auth_token"], owner["workspace_id"]
        self.project = ProjectService(self.store).create_project(self.token,
            workspace_id=self.workspace_id, name="匿名创意", category_id=None, expected_workspace_version=0)

    def repository(self, **changes):
        return CreativeDraftRepository(self.store, artifact_root=self.root,
            **{"auth_token": self.token, "workspace_id": self.workspace_id,
                "project_id": self.project["project_id"], **changes})

    def create(self):
        repo = self.repository()
        with repo.transaction(expected_project_version=0, expected_revision=0):
            draft = VersionReviewService(repo).create_draft(workspace_id=self.workspace_id,
                project_id=self.project["project_id"], base_version_id=None, actor_id=repo.actor_id,
                shots=[{"shot_no": str(index), "shot_id": f"block-{index}",
                    "fields": {"画面描述": f"场景{index}", "声音": "环境声"},
                    "source_refs": [{"origin": "user_input", "input_sha256": "a" * 64}]}
                    for index in range(1, 4)])
        return draft

    def test_no_formal_version_edit_undo_redo_and_restart_keep_source_and_stable_ids(self):
        draft = self.create()
        repo = self.repository()
        with repo.transaction(expected_project_version=1, expected_revision=1):
            VersionReviewService(repo).apply_agent_change_set(draft.draft_id,
                expected_revision=0, actor_id=repo.actor_id,
                operations=[{"shot_no": "2", "field": "画面描述", "value": "人物进入会场"},
                    {"shot_no": "3", "field": "声音", "value": "掌声"}])
        reloaded = self.repository().read()
        self.assertEqual(reloaded["revision"], 2)
        self.assertEqual(reloaded["body"]["draft"]["revision"], 1)
        self.assertTrue(reloaded["body"]["draft"]["shots"][1]["image_reconfirmation_required"])
        with repo.transaction(expected_project_version=2, expected_revision=2):
            VersionReviewService(repo).undo(draft.draft_id, expected_revision=1, actor_id=repo.actor_id)
        with repo.transaction(expected_project_version=3, expected_revision=3):
            VersionReviewService(repo).redo(draft.draft_id, expected_revision=2, actor_id=repo.actor_id)
        final = self.repository().read()["body"]["draft"]
        self.assertIsNone(final["base_version_id"])
        self.assertEqual(final["shots"][1]["shot_id"], "block-2")
        self.assertEqual(final["shots"][1]["source_refs"], draft.shots[1]["source_refs"])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM project_versions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM version_review_drafts").fetchone()[0], 0)

    def test_stale_generation_and_invalid_batch_cannot_overwrite_complete_head(self):
        draft = self.create()
        repo = self.repository()
        before = repo.read()
        with self.assertRaises(VersionReviewError):
            with repo.transaction(expected_project_version=1, expected_revision=1):
                VersionReviewService(repo).apply_agent_change_set(draft.draft_id,
                    expected_revision=0, actor_id=repo.actor_id, operations=[
                        {"shot_no": "1", "field": "画面描述", "value": "新画面"},
                        {"shot_no": "99", "field": "画面描述", "value": "无效"}])
        self.assertEqual(repo.read(), before)
        with self.assertRaises(WorkspaceServiceError):
            with repo.transaction(expected_project_version=1, expected_revision=0):
                repo.body["draft"] = None
        self.assertEqual(repo.read(), before)

    def test_authentication_precedes_artifact_reads_and_writes(self):
        self.create()
        repo = self.repository(auth_token="invalid")
        with self.assertRaises(WorkspaceServiceError):
            repo.read()
        with self.assertRaises(WorkspaceServiceError):
            with repo.transaction(expected_project_version=1, expected_revision=1):
                self.fail("must not enter unauthorized transaction")

    def test_legacy_manual_patch_recovers_stable_identity_after_delete_and_undo(self):
        draft = self.create()
        repo = self.repository()
        with repo.transaction(expected_project_version=1, expected_revision=1):
            VersionReviewService(repo).apply_table_patch(draft.draft_id, expected_revision=0,
                actor_id=repo.actor_id, shot_no="2", field="声音", value="用户保留音乐")
        with repo.transaction(expected_project_version=2, expected_revision=2):
            VersionReviewService(repo).delete_shot(draft.draft_id, expected_revision=1,
                actor_id=repo.actor_id, shot_no="1", origin="table")
        body = repo.read()["body"]
        self.assertEqual(body["draft"]["shots"][0]["shot_id"], "block-2")
        self.assertEqual(repo.manual_values(body), {"block-2": {"声音": "用户保留音乐"}})
        for version in (3, 4):
            with repo.transaction(expected_project_version=version, expected_revision=version):
                VersionReviewService(repo).undo(draft.draft_id, expected_revision=version - 1, actor_id=repo.actor_id)
        self.assertEqual(repo.manual_values(repo.read()["body"]), {})


if __name__ == "__main__":
    unittest.main()
