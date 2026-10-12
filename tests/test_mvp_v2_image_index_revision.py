from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as SheetImage
from PIL import Image

from mvp_v2.asset_package import create_asset_package, bundle_asset_package_v3, load_asset_package
from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.package_publication import AssetPackagePublicationService, PackagePublicationError
from mvp_v2.package_registry import LocalPackageRegistry, build_package_record
from mvp_v2.run_contract import sha256_file
from tests.test_mvp_v2_asset_package import _write_source_with_reference_image


class ImageIndexRevisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / "source"
        source.mkdir()
        workbook = source / "M+发布会类通用脚本库.xlsx"
        _write_source_with_reference_image(workbook, self.root / "one.png")
        Image.new("RGB", (64, 48), "green").save(self.root / "two.png")
        book = load_workbook(workbook)
        book["创意库"].add_image(SheetImage(self.root / "two.png"), "D7")
        book.save(workbook)
        book.close()
        seed = create_asset_package(self.root / "seed", package_id="anonymous", source_root=source)
        manifest = bundle_asset_package_v3(seed, self.root / "packages/anonymous/v1")
        self.record = build_package_record(manifest)
        self.registry = LocalPackageRegistry(self.root / "registry.json")
        self.registry.register_records([self.record], promote_formal=True)
        self.service = AssetPackagePublicationService(
            registry=self.registry, storage=LocalAssetStorage(self.root / "storage"), packages_root=self.root / "packages",
        )
        package = load_asset_package(manifest)
        self.old_index = package["index_path"]
        self.old_index.chmod(0o444)
        self.before = sha256_file(self.old_index)
        self.updates = []
        with sqlite3.connect(self.old_index) as connection:
            for media_id, digest in connection.execute("SELECT media_id, content_sha256 FROM media_assets ORDER BY media_id"):
                sources = [dict(zip(("file", "sheet", "row"), row)) for row in connection.execute(
                    "SELECT DISTINCT source_file, source_sheet, source_row FROM media_script_links WHERE media_id=?", (media_id,),
                )]
                self.updates.append({"media_id": media_id, "content_sha256": digest, "sources": sources,
                    "analysis": {"caption": "匿名现场人物", "tags": ["现场"], "main_subject": "人物",
                                 "quality_flags": {"low_resolution": False}, "prompt_version": "anonymous-v1"}})
        self.assertEqual(len(self.updates), 2)

    def test_new_index_is_reversible_and_keeps_every_media_source_identity(self):
        aliases = deepcopy(self.registry.list()["aliases"])
        new = self.service.prepare_image_analysis_revision(category_id="launch", source_ref="anonymous@1", updates=self.updates)
        self.assertEqual(new["package_version"], 2)
        self.assertEqual(self.registry.list()["aliases"], aliases)
        self.assertEqual(sha256_file(self.old_index), self.before)
        package = load_asset_package(new["local_path"])
        self.assertEqual(package["index_path"].stat().st_mode & 0o777, 0o444)
        with sqlite3.connect(self.old_index) as old, sqlite3.connect(package["index_path"]) as current:
            for table in ("media_script_links", "media_locations", "script_assets"):
                self.assertEqual(old.execute(f"SELECT * FROM {table}").fetchall(), current.execute(f"SELECT * FROM {table}").fetchall())
            self.assertEqual(old.execute("SELECT media_id,content_sha256,current_path FROM media_assets ORDER BY media_id").fetchall(), current.execute("SELECT media_id,content_sha256,current_path FROM media_assets ORDER BY media_id").fetchall())
            for (payload,) in current.execute("SELECT analysis_json FROM media_assets"):
                self.assertEqual(json.loads(payload)["quality_flags"], [])
                self.assertEqual(json.loads(payload)["main_subject"], "人物")
        replay = self.service.prepare_image_analysis_revision(category_id="launch", source_ref="anonymous@1", updates=self.updates)
        self.assertEqual(replay["package_sha256"], new["package_sha256"])
        self.registry.promote("launch", "anonymous", 2)
        self.registry.restore_alias("launch", "anonymous@2", "anonymous@1")
        self.assertEqual(self.registry.resolve_formal("launch")["package_version"], 1)

    def test_bad_identity_or_source_rejects_whole_batch_without_new_package(self):
        for target in ("content_sha256", "sources"):
            with self.subTest(target=target):
                updates = deepcopy(self.updates)
                updates[1][target] = "0" * 64 if target == "content_sha256" else [{"file": "wrong.xlsx", "sheet": "创意库", "row": 99}]
                with self.assertRaises(PackagePublicationError):
                    self.service.prepare_image_analysis_revision(category_id="launch", source_ref="anonymous@1", updates=updates)
                self.assertFalse((self.root / "packages/anonymous/v2").exists())
                self.assertEqual(sha256_file(self.old_index), self.before)
