import os
import unittest
from unittest.mock import patch

from mvp_v2.command import _session, parser


class CommandCloudTests(unittest.TestCase):
    def test_cloud_run_waits_by_default_and_async_is_explicit(self):
        values = parser().parse_args([
            "run", "cloud",
            "--project-id", "project_12345678",
            "--brief", "/tmp/brief.xlsx",
            "--package", "launch-core",
        ])
        self.assertFalse(values.async_mode)
        self.assertEqual(values.package, "launch-core")

        async_values = parser().parse_args([
            "run", "cloud",
            "--project-id", "project_12345678",
            "--brief", "/tmp/brief.xlsx",
            "--package", "launch-core@4",
            "--async",
            "--source-run-id", "legacy-1",
        ])
        self.assertTrue(async_values.async_mode)
        self.assertEqual(async_values.source_run_id, "legacy-1")

    def test_execute_backend_is_explicit_and_cloud_never_falls_back(self):
        values = parser().parse_args([
            "run", "execute",
            "--project-id", "project_12345678",
            "--brief", "brief.xlsx",
            "--package", "launch-core@formal",
            "--cloud",
        ])
        self.assertTrue(values.cloud)
        self.assertFalse(values.local)
        self.assertEqual(values.package, "launch-core@formal")

        values = parser().parse_args(["run", "status", "--run-id", "run_123"])
        self.assertEqual(values.run_id, "run_123")
        values = parser().parse_args([
            "run", "download", "--run-id", "run_123",
            "--artifact-path", "output/delivery.xlsx", "--output", "/tmp/delivery.xlsx",
        ])
        self.assertEqual(values.artifact_path, "output/delivery.xlsx")

    def test_version_and_release_status_are_public_commands(self):
        self.assertEqual(parser().parse_args(["version"]).command, "version")
        values = parser().parse_args(["admin", "release", "status"])
        self.assertEqual(values.release_command, "status")

        values = parser().parse_args(["admin", "cloud", "migrate", "--check"])
        self.assertEqual(values.cloud_command, "migrate")
        self.assertTrue(values.check)

        values = parser().parse_args(["admin", "package", "usage", "--cloud"])
        self.assertTrue(values.cloud)

        values = parser().parse_args([
            "admin", "package", "rollback", "--category-id", "launch", "--cloud"
        ])
        self.assertEqual(values.category_id, ["launch"])

    def test_remote_session_requires_token_and_workspace_as_a_pair(self):
        with patch.dict(
            os.environ,
            {
                "MPLUS_AGENT_AUTH_TOKEN": "secret-session-token",
                "MPLUS_WORKSPACE_ID": "workspace_12345678",
            },
            clear=False,
        ):
            self.assertEqual(
                _session(),
                ("secret-session-token", "workspace_12345678"),
            )
        with patch.dict(
            os.environ,
            {"MPLUS_AGENT_AUTH_TOKEN": "secret-session-token", "MPLUS_WORKSPACE_ID": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "必须同时配置"):
                _session()


if __name__ == "__main__":
    unittest.main()
