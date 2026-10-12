import json
from pathlib import Path
import tempfile
import unittest

from mvp_v2.asset_package import bundle_asset_package_v3, create_asset_package, load_asset_package
from mvp_v2.package_registry import build_package_record
from mvp_v2.run_provenance import RunProvenanceError, run_provenance
from tests.test_mvp_v2_asset_package import _write_source


class RunProvenanceTests(unittest.TestCase):
    def _fixture(self, root: Path):
        source = root / "source"
        source.mkdir()
        _write_source(source / "M+发布会类通用脚本库.xlsx")
        editable = create_asset_package(root / "editable", package_id="launch-core", source_root=source)
        package = bundle_asset_package_v3(editable, root / "package", package_version=4)
        brief = root / "brief.txt"
        brief.write_text("项目名称：匿名项目\n脚本类型：发布会\n", encoding="utf-8")
        return package, brief

    def test_cloud_lock_matches_full_package_sha_and_materialized_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            package, brief = self._fixture(Path(tmp))
            loaded = load_asset_package(package)
            local = run_provenance(
                input_path=brief,
                asset_package=package,
                environ={"MPLUS_SOURCE_COMMIT": "a" * 40},
            )
            self.assertEqual(
                local["package_sha256"],
                build_package_record(package)["package_sha256"],
            )
            locked = {
                "schema_version": "mplus-cloud-run/v1",
                "cloud_run_id": "run_cloud_1",
                "runner_version": local["runner_version"],
                "source_commit": "a" * 40,
                "package_id": loaded["package_id"],
                "package_version": 4,
                "package_sha256": local["package_sha256"],
                "package_schema_version": loaded["schema_version"],
                "input_ref": f"cos://private/input?sha256={local['input_sha256']}",
                "input_sha256": local["input_sha256"],
            }
            result = run_provenance(
                input_path=brief,
                asset_package=package,
                environ={
                    "MPLUS_SOURCE_COMMIT": "a" * 40,
                    "MPLUS_LOCKED_RUN_PROVENANCE": json.dumps(locked),
                },
            )

            self.assertEqual(result["package_sha256"], local["package_sha256"])
            self.assertEqual(result["package_manifest_sha256"], local["package_manifest_sha256"])
            self.assertEqual(result["materialized_input_path"], str(brief.resolve()))

    def test_cloud_lock_rejects_input_or_package_version_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            package, brief = self._fixture(Path(tmp))
            local = run_provenance(
                input_path=brief,
                asset_package=package,
                environ={"MPLUS_SOURCE_COMMIT": "a" * 40},
            )
            locked = {
                "schema_version": "mplus-cloud-run/v1",
                "cloud_run_id": "run_cloud_1",
                "runner_version": local["runner_version"],
                "source_commit": "a" * 40,
                "package_id": "launch-core",
                "package_version": 99,
                "package_sha256": "b" * 64,
                "package_schema_version": "mplus-asset-package/v3",
                "input_ref": "cos://private/input",
                "input_sha256": local["input_sha256"],
            }
            with self.assertRaisesRegex(RunProvenanceError, "package_version"):
                run_provenance(
                    input_path=brief,
                    asset_package=package,
                    environ={
                        "MPLUS_SOURCE_COMMIT": "a" * 40,
                        "MPLUS_LOCKED_RUN_PROVENANCE": json.dumps(locked),
                    },
                )

    def test_cloud_lock_rejects_full_package_sha_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            package, brief = self._fixture(Path(tmp))
            local = run_provenance(
                input_path=brief,
                asset_package=package,
                environ={"MPLUS_SOURCE_COMMIT": "a" * 40},
            )
            locked = {
                "schema_version": "mplus-cloud-run/v1",
                "cloud_run_id": "run_cloud_1",
                "runner_version": local["runner_version"],
                "source_commit": "a" * 40,
                "package_id": "launch-core",
                "package_version": 4,
                "package_sha256": "b" * 64,
                "package_schema_version": "mplus-asset-package/v3",
                "input_ref": "cos://private/input",
                "input_sha256": local["input_sha256"],
            }
            with self.assertRaisesRegex(RunProvenanceError, "package_sha256"):
                run_provenance(
                    input_path=brief,
                    asset_package=package,
                    environ={
                        "MPLUS_SOURCE_COMMIT": "a" * 40,
                        "MPLUS_LOCKED_RUN_PROVENANCE": json.dumps(locked),
                    },
                )


if __name__ == "__main__":
    unittest.main()
