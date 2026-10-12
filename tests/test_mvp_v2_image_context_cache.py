from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mvp_v2.version_delivery as version_delivery
from mvp_v2.workspace_store import WorkspaceServiceError
from tests.test_mvp_v2_version_delivery import VersionDeliveryFixture


class ImageContextCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = VersionDeliveryFixture(Path(temporary.name))
        fixture = self.fixture
        project = fixture.create_project("匿名图片上下文")
        self.version, self.run_dir = fixture.create_version(project)
        self.project = fixture.projects.get_project(fixture.owner_token, project["project_id"])

    def resolve(self, token=None):
        return self.fixture.delivery.resolve(token or self.fixture.owner_token,
            self.project["project_id"], self.version["version_id"],
            expected_project_version=self.project["project_version"], reuse_immutable=True)

    def test_repeated_preview_context_only_parses_verified_stages_once(self):
        with patch.object(self.fixture.delivery, "_validate_stage_chain", wraps=self.fixture.delivery._validate_stage_chain) as validate:
            first = self.resolve()
            self.assertEqual(first, self.resolve())
        self.assertEqual(validate.call_count, 1)

    def test_cached_context_still_rejects_stage_drift_and_revoked_authority(self):
        self.resolve()
        with self.assertRaises(WorkspaceServiceError):
            self.resolve("invalid-token")
        stage = self.run_dir / "script_draft.json"
        stage.write_text(stage.read_text() + " ")
        with self.assertRaisesRegex(WorkspaceServiceError, "HASH_MISMATCH"):
            self.resolve()

    def test_a_file_changed_between_initial_hash_and_cache_fingerprint_is_rejected(self):
        original = version_delivery._immutable_file_fingerprint
        changed = False

        def change_before_fingerprint(paths):
            nonlocal changed
            if not changed:
                changed = True
                path = self.run_dir / "script_draft.json"
                path.write_text(path.read_text() + " ")
            return original(paths)

        with patch.object(version_delivery, "_immutable_file_fingerprint", side_effect=change_before_fingerprint):
            with self.assertRaisesRegex(WorkspaceServiceError, "HASH_MISMATCH"):
                self.resolve()
