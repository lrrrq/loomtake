from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PIL import Image

from mvp_v2.asset_center_tools import AssetCenterToolService
from mvp_v2.asset_package import bundle_asset_package_v2, create_asset_package
from mvp_v2.package_registry import LocalPackageRegistry, build_package_record
from mvp_v2.ui_workbench import _visible_imports
from mvp_v2.workspace_store import WorkspaceServiceError
from tests.test_mvp_v2_asset_center import _write_source_workbook


class AssetRuntimeRegistryTests(unittest.TestCase):
    def test_new_import_catalog_is_visible_as_empty_without_creating_a_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            class UnusedAssetCenter:
                catalog_path = Path(temporary) / "asset_imports.sqlite3"

                def get_import(self, import_id):
                    raise AssertionError("全新目录没有导入记录可读")

            center = UnusedAssetCenter()
            self.assertEqual(_visible_imports(center, category_id="launch", project_id="project_new"), [])
            self.assertFalse(center.catalog_path.exists())

    def test_corrupt_import_catalog_is_not_misreported_as_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            class CorruptAssetCenter:
                catalog_path = Path(temporary) / "asset_imports.sqlite3"

            center = CorruptAssetCenter()
            center.catalog_path.write_bytes(b"corrupt-existing-import-catalog")
            with self.assertRaises(WorkspaceServiceError) as caught:
                _visible_imports(center, category_id="launch", project_id="project_new")
            self.assertEqual(caught.exception.code, "INTERNAL_ERROR")
            self.assertEqual(center.catalog_path.read_bytes(), b"corrupt-existing-import-catalog")

    def test_runtime_registry_outside_package_ancestors_supports_import(self):
        """Configured registry is authoritative even when package paths differ."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            _write_source_workbook(source / "M+发布会类通用脚本库.xlsx")
            editable = create_asset_package(
                root / "editable", package_id="anonymous-runtime-launch",
                source_root=source, category_id="launch",
            )
            manifest = bundle_asset_package_v2(
                editable, root / "external-package" / "v1", package_version=1,
            )
            registry = LocalPackageRegistry(root / "runtime" / "configured-registry.json")
            registry.register_records([build_package_record(manifest)], promote_formal=True)
            runtime = {
                "data_root": str(root / "runtime"),
                "asset_package": str(manifest),
                "asset_packages": {"发布会": str(manifest)},
                "package_registry_path": str(registry.path),
                "packages_root": str(root / "published-versions"),
            }
            tools = AssetCenterToolService.from_runtime_config(
                confirmation_signing_key=b"anonymous-runtime-signing-key-1234", config=runtime,
            )
            image = root / "anonymous.png"
            Image.new("RGB", (16, 16), (37, 82, 124)).save(image)
            frozen = tools.freeze_upload(image)
            center = tools.services.packages.resolve_active("launch").center
            record = center.stage_import(
                import_id="import_runtime_registry", category_id="launch", scope="shared",
                files=[tools.services.uploads.as_import_file(frozen["artifact_ref"])],
                requested_by_actor_id="actor_anonymous", expected_package_version=1,
            )
            self.assertEqual(record["state"], "staged")
            self.assertEqual(record["machine_review_status"], "passed")
            self.assertEqual(record["items"][0]["content_sha256"], frozen["content_sha256"])
            self.assertEqual(center.catalog_path, registry.path.parent / "asset_imports.sqlite3")
            self.assertEqual(center.package_publication.packages_root, (root / "published-versions").resolve())
            promoted = bundle_asset_package_v2(editable, root / "external-package" / "v2", package_version=2)
            registry.register_records([build_package_record(promoted)], promote_formal=True)
            active = tools.services.packages.resolve_active("launch")
            self.assertEqual(active.package_version, 2)
            next_record = active.center.stage_import(
                import_id="import_after_promotion", category_id="launch", scope="shared",
                files=[tools.services.uploads.as_import_file(frozen["artifact_ref"])],
                requested_by_actor_id="actor_anonymous", expected_package_version=active.package_version,
            )
            self.assertEqual(next_record["state"], "staged")
            self.assertEqual(next_record["package_version"], 2)


if __name__ == "__main__":
    unittest.main()
