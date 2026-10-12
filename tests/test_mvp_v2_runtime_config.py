import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mvp_v2.runtime_config import (
    configured_profile,
    configured_source_commit,
    configured_asset_package,
    default_profile_port,
    legacy_roots,
    load_local_secrets,
    load_runtime_config,
    remap_legacy_path,
)


class RuntimeConfigTests(unittest.TestCase):
    def test_profiles_isolate_default_paths_and_ports(self):
        with patch.dict(os.environ, {"MPLUS_PROFILE": "dev"}, clear=False):
            for name in ("MPLUS_CONFIG", "MPLUS_DATA_ROOT", "MPLUS_SOURCE_ROOT"):
                os.environ.pop(name, None)
            result = load_runtime_config()

        self.assertEqual(result["profile"], "dev")
        self.assertEqual(result["execution_backend"], "local")
        self.assertEqual(result["default_port"], 8766)
        self.assertTrue(result["data_root"].endswith("/.mplus/profiles/dev/data"))
        self.assertTrue(result["package_registry_path"].endswith("/profiles/dev/data/package_registry.json"))
        self.assertEqual(configured_profile({}), "stable")
        self.assertEqual(default_profile_port("stable"), 8765)

    def test_profile_and_backend_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "stable"):
            configured_profile({"MPLUS_PROFILE": "production"})
        with patch.dict(
            os.environ,
            {"MPLUS_PROFILE": "test", "MPLUS_EXECUTION_BACKEND": "automatic"},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "local 或 cloud"):
                load_runtime_config()

    def test_runtime_paths_derive_from_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "mplus-data"
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                '{"data_root": "' + str(data_root) + '", "custom": "kept"}',
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"MPLUS_CONFIG": str(config_path)},
                clear=False,
            ):
                os.environ.pop("MPLUS_DATA_ROOT", None)
                os.environ.pop("MPLUS_SOURCE_ROOT", None)
                result = load_runtime_config()

        self.assertEqual(result["data_root"], str(data_root.resolve()))
        self.assertEqual(result["source_root"], str(data_root.resolve() / "sources"))
        self.assertEqual(result["runs_root"], str(data_root.resolve() / "runs"))
        self.assertEqual(result["cache_root"], str(data_root.resolve() / "cache"))
        self.assertEqual(result["exports_root"], str(data_root.resolve() / "exports"))
        self.assertEqual(result["logs_root"], str(data_root.resolve() / "logs"))
        self.assertEqual(result["packages_root"], str(data_root.resolve() / "packages"))
        self.assertEqual(result["legacy_mounts_path"], str(data_root.resolve() / "legacy-mounts.json"))
        self.assertEqual(result["custom"], "kept")

    def test_source_root_environment_overrides_config_without_project_path_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({
                    "data_root": str(root / "data"),
                    "source_root": str(root / "configured-source"),
                }),
                encoding="utf-8",
            )
            override = root / "mounted-source"
            with patch.dict(os.environ, {
                "MPLUS_CONFIG": str(config_path),
                "MPLUS_SOURCE_ROOT": str(override),
            }, clear=False):
                result = load_runtime_config()

        self.assertEqual(result["source_root"], str(override.resolve()))
        self.assertNotIn("/Users/example/private-fixture.txt", result["source_root"])

    def test_source_commit_is_full_sha_or_unavailable(self):
        self.assertEqual(configured_source_commit({}), "unavailable")
        self.assertEqual(
            configured_source_commit({"MPLUS_SOURCE_COMMIT": "A" * 40}),
            "a" * 40,
        )
        for invalid in ("abc123", "g" * 40, "a" * 41):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "40 位 Git SHA"):
                    configured_source_commit({"MPLUS_SOURCE_COMMIT": invalid})

    def test_data_root_environment_overrides_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text('{"data_root": "/configured"}', encoding="utf-8")
            override = Path(tmp) / "override"
            with patch.dict(
                os.environ,
                {
                    "MPLUS_CONFIG": str(config_path),
                    "MPLUS_DATA_ROOT": str(override),
                },
                clear=False,
            ):
                result = load_runtime_config()

        self.assertEqual(result["data_root"], str(override.resolve()))

    def test_configured_asset_package_prefers_category_mapping(self):
        config = {
            "asset_package": "/packages/legacy-launch",
            "asset_packages": {
                "发布会": "/packages/launch-v2",
                "展会": "/packages/exhibition-v2",
            },
        }

        self.assertEqual(
            configured_asset_package("发布会", config),
            "/packages/launch-v2",
        )
        self.assertEqual(
            configured_asset_package("展会", config),
            "/packages/exhibition-v2",
        )
        self.assertEqual(configured_asset_package("年会", config), "")

    def test_read_only_legacy_mount_remaps_missing_old_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_root = root / "data"
            target = root / "archive" / "runs"
            target.mkdir(parents=True)
            (data_root / "legacy-mounts.json").parent.mkdir(parents=True)
            (data_root / "legacy-mounts.json").write_text(
                '{"mounts":[{"kind":"runs","mode":"read_only",'
                '"original_path":"/old/runs","path":"' + str(target) + '"}]}',
                encoding="utf-8",
            )
            config = {
                "legacy_mounts_path": str(data_root / "legacy-mounts.json"),
            }

            self.assertEqual(legacy_roots("runs", config), [target.resolve()])
            self.assertEqual(
                remap_legacy_path("/old/runs/project/run.xlsx", config),
                target.resolve() / "project" / "run.xlsx",
            )

    def test_local_secrets_loads_only_whitelisted_missing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.env"
            path.write_text(
                "# local only\n"
                "DEEPSEEK_API_KEY='local-deepseek'\n"
                "MINIMAX_API_KEY=local-minimax\n"
                "UNRELATED_SECRET=must-not-load\n"
                "malformed\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"MINIMAX_API_KEY": "existing-minimax"},
                clear=False,
            ):
                os.environ.pop("DEEPSEEK_API_KEY", None)
                os.environ.pop("UNRELATED_SECRET", None)
                result = load_local_secrets(path)

                self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "local-deepseek")
                self.assertEqual(os.environ["MINIMAX_API_KEY"], "existing-minimax")
                self.assertNotIn("UNRELATED_SECRET", os.environ)
                self.assertEqual(result["loaded"], ["DEEPSEEK_API_KEY"])
                self.assertEqual(result["ignored"], ["UNRELATED_SECRET", "malformed"])
                self.assertNotIn("local-deepseek", repr(result))

    def test_image_provider_key_uses_the_same_local_secret_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image-secrets.env"
            path.write_text("ARK_API_KEY=anonymous-fixture-value\nUNRELATED_SECRET=must-not-load\n")
            with patch.dict(os.environ, {}, clear=True):
                result = load_local_secrets(path)
                self.assertEqual(os.environ.get("ARK_API_KEY"), "anonymous-fixture-value")
                self.assertNotIn("UNRELATED_SECRET", os.environ)
                self.assertNotIn("anonymous-fixture-value", repr(result))

    def test_missing_secret_file_is_non_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = load_local_secrets(Path(tmp) / "missing.env")

        self.assertEqual(result["loaded"], [])
        self.assertEqual(result["ignored"], [])


if __name__ == "__main__":
    unittest.main()
