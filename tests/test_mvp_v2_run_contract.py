import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openpyxl
import PIL

from mvp_v2.run_contract import (
    build_baseline_manifest,
    sha256_file,
    sha256_json,
    write_baseline_manifest,
)


class RunContractTests(unittest.TestCase):
    def test_sha256_file_reads_finite_chunks_until_eof(self):
        payload = b"a" * (2 * 1024 * 1024 + 7)

        class ControlledFile:
            def __init__(self):
                self.offset = 0
                self.read_sizes = []
                self.eof_read_count = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size):
                self.read_sizes.append(size)
                if self.offset >= len(payload):
                    self.eof_read_count += 1
                    return b""
                chunk = payload[self.offset:self.offset + size]
                self.offset += len(chunk)
                return chunk

        class ControlledPath:
            def __init__(self, file):
                self.file = file
                self.open_modes = []

            def open(self, mode):
                self.open_modes.append(mode)
                return self.file

            def read_bytes(self):
                raise AssertionError("sha256_file must not use read_bytes")

        controlled_file = ControlledFile()
        controlled_path = ControlledPath(controlled_file)

        with patch("mvp_v2.run_contract.Path", return_value=controlled_path):
            digest = sha256_file("controlled.bin")

        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        self.assertEqual(controlled_path.open_modes, ["rb"])
        self.assertGreater(len(controlled_file.read_sizes), 2)
        self.assertTrue(all(
            isinstance(size, int) and 0 < size < len(payload)
            for size in controlled_file.read_sizes
        ))
        self.assertEqual(controlled_file.offset, len(payload))
        self.assertEqual(controlled_file.eof_read_count, 1)

    def test_sha256_json_uses_utf8_sorted_keys_and_stable_separators(self):
        value = {"z": [2, 1], "a": "中文"}

        self.assertEqual(
            sha256_json(value),
            hashlib.sha256(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )

    def test_manifest_is_stable_sorted_and_redacts_nested_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            (root / "source" / "nested").mkdir(parents=True)
            (root / "input").mkdir()
            (root / "source" / "a.py").write_text("print('a')\n", encoding="utf-8")
            (root / "source" / "nested" / "b.py").write_text("print('b')\n", encoding="utf-8")
            (root / "source" / "__pycache__").mkdir()
            (root / "source" / "__pycache__" / "skip.pyc").write_bytes(b"skip")
            (root / "source" / "__pycache__" / "skip.txt").write_text("skip", encoding="utf-8")
            (root / "source" / "standalone.pyc").write_bytes(b"skip")
            (root / "source" / ".DS_Store").write_bytes(b"skip")
            (root / "input" / "brief.json").write_text('{"name":"launch"}', encoding="utf-8")
            config = {
                "api_token": "token-value",
                "nested": [{"password": "password-value"}],
                "provider": "minimax",
                "model_label": "visible-model-label",
            }

            first = build_baseline_manifest(
                root,
                [root / "source"],
                [root / "input" / "brief.json"],
                config,
            )
            second = build_baseline_manifest(
                root,
                [root / "source"],
                [root / "input" / "brief.json"],
                config,
            )

            self.assertEqual(first["schema_version"], "v0.2.3")
            self.assertEqual(first["baseline_sha256"], second["baseline_sha256"])
            self.assertEqual(
                [(row["role"], row["path"]) for row in first["files"]],
                [
                    ("input", "input/brief.json"),
                    ("source", "source/a.py"),
                    ("source", "source/nested/b.py"),
                ],
            )
            self.assertTrue(all(set(row) == {"role", "path", "sha256", "size_bytes"} for row in first["files"]))
            self.assertEqual(first["config"]["api_token"], "[REDACTED]")
            self.assertEqual(first["config"]["nested"][0]["password"], "[REDACTED]")
            self.assertEqual(first["config"]["provider"], "minimax")
            self.assertEqual(first["config"]["model_label"], "visible-model-label")
            manifest_text = json.dumps(first, ensure_ascii=False)
            self.assertNotIn("token-value", manifest_text)
            self.assertNotIn("password-value", manifest_text)

    def test_manifest_has_exact_contract_fields_and_visible_provider_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            source = root / "source.py"
            source.write_text("source", encoding="utf-8")
            config = {
                "provider": "provider-managed",
                "model_label": "visible-model-label",
            }

            manifest = build_baseline_manifest(root, [source], config=config)

            self.assertEqual(set(manifest), {
                "schema_version",
                "source_commit",
                "python_version",
                "dependencies",
                "files",
                "config",
                "config_sha256",
                "baseline_sha256",
            })
            self.assertEqual(
                manifest["dependencies"],
                {
                    "openpyxl": openpyxl.__version__,
                    "Pillow": PIL.__version__,
                },
            )
            self.assertEqual(manifest["config"], config)
            self.assertEqual(set(manifest["config"]), {"provider", "model_label"})

    def test_manifest_records_and_hashes_configured_source_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            source = root / "source.py"
            source.write_text("source", encoding="utf-8")
            with patch.dict(os.environ, {"MPLUS_SOURCE_COMMIT": "a" * 40}, clear=False):
                first = build_baseline_manifest(root, [source])
            with patch.dict(os.environ, {"MPLUS_SOURCE_COMMIT": "b" * 40}, clear=False):
                second = build_baseline_manifest(root, [source])

        self.assertEqual(first["source_commit"], "a" * 40)
        self.assertEqual(second["source_commit"], "b" * 40)
        self.assertNotEqual(first["baseline_sha256"], second["baseline_sha256"])

    def test_baseline_hash_changes_with_python_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            source = root / "source.py"
            source.write_text("source", encoding="utf-8")

            with patch("mvp_v2.run_contract.platform.python_version", return_value="3.11.0"), patch(
                "mvp_v2.run_contract.openpyxl.__version__", "3.1.0"
            ):
                original = build_baseline_manifest(root, [source])
            with patch("mvp_v2.run_contract.platform.python_version", return_value="3.12.0"), patch(
                "mvp_v2.run_contract.openpyxl.__version__", "3.1.0"
            ):
                python_changed = build_baseline_manifest(root, [source])

            self.assertNotEqual(
                original["baseline_sha256"],
                python_changed["baseline_sha256"],
            )

    def test_baseline_hash_changes_with_openpyxl_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            source = root / "source.py"
            source.write_text("source", encoding="utf-8")

            with patch("mvp_v2.run_contract.platform.python_version", return_value="3.11.0"), patch(
                "mvp_v2.run_contract.openpyxl.__version__", "3.1.0"
            ):
                original = build_baseline_manifest(root, [source])
            with patch("mvp_v2.run_contract.platform.python_version", return_value="3.11.0"), patch(
                "mvp_v2.run_contract.openpyxl.__version__", "3.2.0"
            ):
                dependency_changed = build_baseline_manifest(root, [source])

            self.assertNotEqual(
                original["baseline_sha256"],
                dependency_changed["baseline_sha256"],
            )

    def test_manifest_dependencies_include_pillow_and_hash_changes_with_its_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            source = root / "source.py"
            source.write_text("source", encoding="utf-8")

            with patch("mvp_v2.run_contract.platform.python_version", return_value="3.11.0"), patch(
                "mvp_v2.run_contract.openpyxl.__version__", "3.1.0"
            ), patch.object(PIL, "__version__", "10.0.0"):
                original = build_baseline_manifest(root, [source])
            with patch("mvp_v2.run_contract.platform.python_version", return_value="3.11.0"), patch(
                "mvp_v2.run_contract.openpyxl.__version__", "3.1.0"
            ), patch.object(PIL, "__version__", "10.1.0"):
                pillow_changed = build_baseline_manifest(root, [source])

            self.assertEqual(
                original["dependencies"],
                {"openpyxl": "3.1.0", "Pillow": "10.0.0"},
            )
            self.assertNotEqual(
                original["baseline_sha256"],
                pillow_changed["baseline_sha256"],
            )

    def test_required_key_slots_are_preserved_and_change_config_and_baseline_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            source = root / "source.py"
            source.write_text("source", encoding="utf-8")

            original = build_baseline_manifest(
                root,
                [source],
                config={"required_key_slots": [1, 5, 9]},
            )
            changed = build_baseline_manifest(
                root,
                [source],
                config={"required_key_slots": [1, 5, 10]},
            )

            self.assertEqual(
                original["config"],
                {"required_key_slots": [1, 5, 9]},
            )
            self.assertNotEqual(original["config_sha256"], changed["config_sha256"])
            self.assertNotEqual(original["baseline_sha256"], changed["baseline_sha256"])

    def test_redaction_recognizes_terminal_sensitive_key_components_in_camel_and_kebab_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            source = root / "source.py"
            source.write_text("source", encoding="utf-8")
            config = {
                "MINIMAX_API_KEY": "minimax-secret",
                "api_token": "token-secret",
                "password": "password-secret",
                "apiKey": "api-key-secret",
                "clientSecret": "client-secret-value",
                "dbPassword": "database-password-value",
                "nested": [{"accessToken": "access-token-value"}],
                "api-key": "kebab-api-key-secret",
                "client-secret": "kebab-client-secret-value",
                "db-password": "kebab-database-password-value",
                "nested-kebab": [{"access-token": "kebab-access-token-value"}],
                "monkey_business": "ordinary-business-value",
                "required_key_slots": [1, 5, 9],
            }

            manifest = build_baseline_manifest(root, [source], config=config)

            self.assertEqual(manifest["config"]["MINIMAX_API_KEY"], "[REDACTED]")
            self.assertEqual(manifest["config"]["api_token"], "[REDACTED]")
            self.assertEqual(manifest["config"]["password"], "[REDACTED]")
            self.assertEqual(manifest["config"]["apiKey"], "[REDACTED]")
            self.assertEqual(manifest["config"]["clientSecret"], "[REDACTED]")
            self.assertEqual(manifest["config"]["dbPassword"], "[REDACTED]")
            self.assertEqual(manifest["config"]["nested"][0]["accessToken"], "[REDACTED]")
            self.assertEqual(manifest["config"]["api-key"], "[REDACTED]")
            self.assertEqual(manifest["config"]["client-secret"], "[REDACTED]")
            self.assertEqual(manifest["config"]["db-password"], "[REDACTED]")
            self.assertEqual(manifest["config"]["nested-kebab"][0]["access-token"], "[REDACTED]")
            self.assertEqual(
                manifest["config"]["monkey_business"],
                "ordinary-business-value",
            )
            self.assertEqual(manifest["config"]["required_key_slots"], [1, 5, 9])
            manifest_text = json.dumps(manifest, ensure_ascii=False)
            for secret in (
                "api-key-secret",
                "client-secret-value",
                "database-password-value",
                "access-token-value",
                "kebab-api-key-secret",
                "kebab-client-secret-value",
                "kebab-database-password-value",
                "kebab-access-token-value",
            ):
                self.assertNotIn(secret, manifest_text)

    def test_manifest_hash_changes_when_source_or_input_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            source = root / "source.py"
            input_file = root / "brief.txt"
            source.write_text("first", encoding="utf-8")
            input_file.write_text("first brief", encoding="utf-8")

            original = build_baseline_manifest(root, [source], [input_file])
            source.write_text("second", encoding="utf-8")
            source_changed = build_baseline_manifest(root, [source], [input_file])
            input_file.write_text("second brief", encoding="utf-8")
            input_changed = build_baseline_manifest(root, [source], [input_file])

            self.assertNotEqual(original["baseline_sha256"], source_changed["baseline_sha256"])
            self.assertNotEqual(source_changed["baseline_sha256"], input_changed["baseline_sha256"])

    def test_manifest_rejects_missing_or_outside_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            inside = root / "inside.txt"
            outside = Path(tmp) / "outside.txt"
            inside.write_text("inside", encoding="utf-8")
            outside.write_text("outside", encoding="utf-8")

            with self.assertRaises(ValueError):
                build_baseline_manifest(root, [outside])
            with self.assertRaises(ValueError):
                build_baseline_manifest(root, [root / "missing.txt"])

    def test_directory_recursion_skips_symlink_to_project_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            source_dir = root / "source"
            source_dir.mkdir(parents=True)
            (source_dir / "real.py").write_text("real", encoding="utf-8")
            shared = root / "shared.py"
            shared.write_text("shared", encoding="utf-8")
            (source_dir / "shared-link.py").symlink_to(shared)

            manifest = build_baseline_manifest(root, [source_dir])

            self.assertEqual(
                [row["path"] for row in manifest["files"]],
                ["source/real.py"],
            )

    def test_directory_recursion_skips_symlink_to_outside_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            source_dir = root / "source"
            source_dir.mkdir(parents=True)
            (source_dir / "real.py").write_text("real", encoding="utf-8")
            outside = Path(tmp) / "outside.py"
            outside.write_text("outside", encoding="utf-8")
            (source_dir / "outside-link.py").symlink_to(outside)

            try:
                manifest = build_baseline_manifest(root, [source_dir])
            except ValueError as error:
                self.fail(f"directory recursion followed an outside symlink: {error}")

            self.assertEqual(
                [row["path"] for row in manifest["files"]],
                ["source/real.py"],
            )

    def test_write_manifest_creates_parent_and_preserves_complete_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            source = root / "source.txt"
            source.write_text("source", encoding="utf-8")
            manifest = build_baseline_manifest(root, [source])
            output = root / "output" / "baseline.json"

            written = write_baseline_manifest(output, manifest)

            self.assertEqual(written, output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), manifest)
            self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_write_manifest_publishes_complete_temp_file_with_path_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output" / "baseline.json"
            manifest = {"schema_version": "v0.2.2", "files": []}
            original_replace = Path.replace
            replace_observations = []

            def observe_replace(temporary_path, target):
                replace_observations.append({
                    "source": temporary_path,
                    "target": target,
                    "same_directory": temporary_path.parent == output.parent,
                    "complete_manifest": json.loads(
                        temporary_path.read_text(encoding="utf-8")
                    ),
                })
                return original_replace(temporary_path, target)

            with patch.object(Path, "replace", autospec=True, side_effect=observe_replace) as replace:
                write_baseline_manifest(output, manifest)

            replace.assert_called_once()
            self.assertEqual(len(replace_observations), 1)
            self.assertEqual(replace_observations[0]["target"], output)
            self.assertTrue(replace_observations[0]["same_directory"])
            self.assertEqual(replace_observations[0]["complete_manifest"], manifest)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), manifest)


if __name__ == "__main__":
    unittest.main()
