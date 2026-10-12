import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mvp_v2.asset_package import bundle_asset_package_v3, create_asset_package
from mvp_v2.package_registry import (
    LocalPackageRegistry,
    PackageRegistryError,
    build_package_record,
    _package_stat_fingerprint,
    parse_package_ref,
    register_formal_packages,
)
from tests.test_mvp_v2_asset_package import _write_source


class PackageRegistryTests(unittest.TestCase):
    def test_fingerprint_rejects_nested_links_and_special_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "media"
            nested.mkdir()
            image = nested / "image.png"
            image.write_bytes(b"anonymous fingerprint fixture")
            before = _package_stat_fingerprint(root)
            self.assertEqual([row[0] for row in before], ["media/image.png"])
            link = nested / "alias.png"
            link.symlink_to(image)
            with self.assertRaisesRegex(PackageRegistryError, "符号链接"):
                _package_stat_fingerprint(root)
            link.unlink()
            pipe = nested / "pipe"
            os.mkfifo(pipe)
            with self.assertRaisesRegex(PackageRegistryError, "非普通文件"):
                _package_stat_fingerprint(root)
            pipe.unlink()
            self.assertEqual(before, _package_stat_fingerprint(root))

    def _package(self, root: Path, *, version: int = 4) -> Path:
        source = root / "source"
        source.mkdir()
        _write_source(source / "M+发布会类通用脚本库.xlsx")
        editable = create_asset_package(
            root / "editable",
            package_id="launch-core",
            source_root=source,
        )
        return bundle_asset_package_v3(
            editable,
            root / "versions" / str(version),
            package_version=version,
        )

    def _publication_registry(self, root):
        first = self._package(root)
        registry = LocalPackageRegistry(root / "registry.json")
        registry.register(first)
        for version in (5, 6):
            registry.register(bundle_asset_package_v3(first, root / "versions" / str(version),
                package_version=version))
        registry.promote("launch", "launch-core", 4)
        return registry

    def test_promotion_receipt_survives_lost_response_restart_and_later_publication(self):
        from mvp_v2 import package_registry as module
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self._publication_registry(root)
            original_write = module.write_json_atomic
            def commit_then_lose_response(*args, **kwargs):
                original_write(*args, **kwargs)
                raise OSError("anonymous response loss after atomic replacement")
            with patch.object(module, "write_json_atomic", side_effect=commit_then_lose_response):
                with self.assertRaisesRegex(OSError, "response loss"):
                    registry.promote("launch", "launch-core", 5, expected_current_ref="launch-core@4",
                        command_id="command-a", request_sha256="a" * 64)
            registry = LocalPackageRegistry(root / "registry.json")
            registry.promote("launch", "launch-core", 6, expected_current_ref="launch-core@5",
                command_id="command-b", request_sha256="b" * 64)
            before = registry.path.read_bytes()
            replayed = registry.promote("launch", "launch-core", 5, expected_current_ref="launch-core@4",
                command_id="command-a", request_sha256="a" * 64)
            self.assertEqual(replayed["package_version"], 5)
            self.assertEqual(replayed["previous_ref"], "launch-core@4")
            self.assertEqual(registry.path.read_bytes(), before)
            self.assertEqual(registry.resolve_formal("launch")["package_version"], 6)
            with self.assertRaisesRegex(PackageRegistryError, "其他请求"):
                registry.promote("launch", "launch-core", 5, expected_current_ref="launch-core@4",
                    command_id="command-a", request_sha256="c" * 64)
            self.assertEqual(registry.path.read_bytes(), before)

    def test_competing_promotions_compare_original_alias_inside_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._publication_registry(Path(tmp))
            def promote(version):
                try:
                    return registry.promote("launch", "launch-core", version,
                        expected_current_ref="launch-core@4", command_id=f"command-{version}",
                        request_sha256=str(version) * 64)
                except PackageRegistryError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(promote, (5, 6)))
            winners = [item for item in outcomes if item is not None]
            self.assertEqual(len(winners), 1)
            self.assertEqual(registry.resolve_formal("launch")["package_version"], winners[0]["package_version"])
            events = [item for item in registry.list()["alias_history"] if item.get("command_id")]
            self.assertEqual(len(events), 1)

    def test_duplicate_promotion_and_precommit_failure_are_recoverable(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._publication_registry(Path(tmp))
            def promote(_=None):
                return registry.promote("launch", "launch-core", 5, expected_current_ref="launch-core@4",
                    command_id="command-once", request_sha256="d" * 64)
            before = registry.path.read_bytes()
            with patch("mvp_v2.package_registry.write_json_atomic", side_effect=OSError("before commit")):
                with self.assertRaises(OSError):
                    promote()
            self.assertEqual(registry.path.read_bytes(), before)
            def concurrent_promote(_):
                try:
                    return promote()
                except PackageRegistryError as exc:
                    self.assertIn("正在更新", str(exc))
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(concurrent_promote, (1, 2)))
            original = next(item for item in results if item is not None)
            self.assertEqual(promote(), original)
            self.assertTrue(all(item is None or item == original for item in results))
            events = [item for item in registry.list()["alias_history"] if item.get("command_id") == "command-once"]
            self.assertEqual(len(events), 1)

    def test_formal_alias_resolves_to_immutable_content_bound_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._package(root)
            registry = LocalPackageRegistry(root / "registry.json")

            record = registry.register(package)
            alias = registry.promote("launch", "launch-core", 4)
            resolved = registry.resolve("launch", "launch-core@formal")

            self.assertEqual(record["package_sha256"], resolved["package_sha256"])
            self.assertEqual(alias["resolved_ref"] if "resolved_ref" in alias else alias["package_id"], "launch-core")
            self.assertEqual(resolved["resolved_ref"], "launch-core@4")
            self.assertEqual(record["file_count"], len(record["files"]))
            self.assertGreater(record["size_bytes"], 0)

    def test_registered_version_fails_closed_after_package_file_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._package(root)
            registry = LocalPackageRegistry(root / "registry.json")
            registry.register(package)
            registry.promote("launch", "launch-core", 4)
            rules = package.parent / "template_rules.json"
            payload = json.loads(rules.read_text(encoding="utf-8"))
            payload["drift"] = True
            rules.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(PackageRegistryError, "SHA"):
                registry.resolve("launch", "launch-core@formal")

    def test_resolve_reuses_full_verification_until_stat_fingerprint_drifts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._package(root)
            registry = LocalPackageRegistry(root / "registry.json")
            registry.register(package)
            registry.promote("launch", "launch-core", 4)

            from mvp_v2 import package_registry
            with patch.object(
                package_registry,
                "build_package_record",
                wraps=package_registry.build_package_record,
            ) as build:
                first = registry.resolve_formal("launch")
                second = LocalPackageRegistry(registry.path).resolve_formal("launch")
                self.assertEqual(first["package_sha256"], second["package_sha256"])
                self.assertEqual(build.call_count, 1)

                rules = package.parent / "template_rules.json"
                payload = json.loads(rules.read_text(encoding="utf-8"))
                payload["drift_after_cache"] = True
                rules.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(PackageRegistryError, "SHA"):
                    registry.resolve_formal("launch")
                self.assertEqual(build.call_count, 2)

    def test_sqlite_runtime_sidecars_are_not_part_of_immutable_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._package(root)
            registry = LocalPackageRegistry(root / "registry.json")
            record = registry.register(package)
            registry.promote("launch", "launch-core", 4)
            (package.parent / "asset_index.sqlite3-shm").write_bytes(b"runtime")
            (package.parent / "asset_index.sqlite3-wal").write_bytes(b"")

            resolved = registry.resolve("launch", "launch-core@formal")

            self.assertEqual(resolved["package_sha256"], record["package_sha256"])
            self.assertFalse(
                any(
                    item["path"].endswith((".sqlite3-shm", ".sqlite3-wal"))
                    for item in resolved["files"]
                )
            )

    def test_parser_requires_formal_or_positive_integer(self):
        self.assertEqual(parse_package_ref("launch-core"), ("launch-core", "formal"))
        self.assertEqual(parse_package_ref("launch-core@4"), ("launch-core", "4"))
        with self.assertRaises(PackageRegistryError):
            parse_package_ref("launch-core@latest")
        with self.assertRaises(PackageRegistryError):
            parse_package_ref("launch-core@0")

    def test_record_rejects_unpublished_v1_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            editable = create_asset_package(
                root / "editable",
                package_id="launch-core",
                source_root=source,
            )
            with self.assertRaisesRegex(PackageRegistryError, "package_version"):
                build_package_record(editable)

    def test_configured_batch_does_not_change_formal_without_explicit_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._package(root)
            registry_path = root / "registry.json"

            register_formal_packages(
                registry_path,
                {"launch": package},
                promote_formal=False,
            )
            registry = LocalPackageRegistry(registry_path).list()

            self.assertEqual(len(registry["packages"]), 1)
            self.assertEqual(registry["aliases"], {})

    def test_batch_category_mismatch_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self._package(root)
            registry_path = root / "registry.json"

            with self.assertRaisesRegex(PackageRegistryError, "配置类别"):
                register_formal_packages(registry_path, {"wrong": package})

            self.assertFalse(registry_path.exists())

    def test_formal_rollback_returns_to_previous_verified_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir()
            second_root.mkdir()
            first = self._package(first_root, version=4)
            second = self._package(second_root, version=5)
            registry = LocalPackageRegistry(root / "registry.json")
            registry.register_records([build_package_record(first)], promote_formal=True)
            registry.register_records([build_package_record(second)], promote_formal=True)

            rollback = registry.rollback_formal(["launch"])

            self.assertEqual(rollback[0]["current_ref"], "launch-core@4")
            self.assertEqual(
                registry.resolve("launch", "launch-core@formal")["package_version"],
                4,
            )


if __name__ == "__main__":
    unittest.main()
