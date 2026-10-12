from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook

from mvp_v2.ai_spec import AI_SPEC_SHEET, check_ai_spec, refresh_ai_spec
from mvp_v2.asset_package import bundle_asset_package_v3, load_asset_package, package_script_catalog
from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.formal_default_images import FormalDefaultImageService
from mvp_v2.package_publication import AssetPackagePublicationService, PackagePublicationError
from mvp_v2.package_registry import LocalPackageRegistry, build_package_record
from mvp_v2.run_contract import sha256_file
from mvp_v2.template_rules import builtin_template_rules
from tests.test_mvp_v2_ai_spec import _write_library


CATEGORIES = ("launch", "exhibition", "annual", "sports", "music_festival", "star_activity")
FIRST_SEGMENTS = (("场地建立", 2), ("嘉宾入场", 2), ("互动体验", 3), ("收束", 1))
SECOND_SEGMENTS = (("场地建立", 1), ("产品展示", 3), ("场地建立", 2), ("收束", 4))


def _ready_template(root: Path, filename: str, segments=FIRST_SEGMENTS, *, source_brand="匿名素材") -> Path:
    """Create an anonymous, reviewed library through the existing AI-spec tool."""
    root.mkdir(parents=True, exist_ok=True)
    original = _write_library(
        root, template_count=sum(count for _, count in segments), creative_count=3,
        with_images=True,
    )
    path = original.rename(root / filename)
    workbook = load_workbook(path)
    try:
        for sheet_name in ("模版", "创意库"):
            for row in workbook[sheet_name].iter_rows(min_row=7):
                row[16].value = source_brand
        template = workbook["模版"]
        source_row = 7
        for structure, count in segments:
            for row in range(source_row, source_row + count):
                template.cell(row, 1, structure if row == source_row else None)
                template.cell(row, 9, f"匿名镜头{row - 6}：工作人员展示活动物料")
            if count > 1:
                template.merge_cells(
                    start_row=source_row, start_column=1,
                    end_row=source_row + count - 1, end_column=1,
                )
            source_row += count
        workbook.save(path)
    finally:
        workbook.close()
    report = refresh_ai_spec(path)
    if not report.in_sync or not report.usable:
        raise AssertionError(f"匿名模板未就绪: {report.validation_codes}")
    return path


def _expected_structures(segments):
    result = []
    start = 1
    for name, count in segments:
        result.append({"name": name, "start": start, "end": start + count - 1, "shot_count": count})
        start += count
    return result


def _remove_brand_origin_column(path: Path) -> None:
    """Represent the old, already-reviewed 32-column workbook format."""
    workbook = load_workbook(path)
    try:
        sheet = workbook[AI_SPEC_SHEET]
        if sheet.cell(1, 33).value != "source_brand_origin":
            raise AssertionError("匿名规范表缺少新增来源标记列")
        sheet.delete_cols(33)
        workbook.save(path)
    finally:
        workbook.close()


class TemplateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = LocalPackageRegistry(self.root / "registry.json")
        self.service = AssetPackagePublicationService(
            storage=LocalAssetStorage(self.root / "objects"),
            registry=self.registry,
            packages_root=self.root / "packages",
        )

    def _import(self, path: Path, category_id="launch", label="匿名模板"):
        return self.service.import_template(
            category_id=category_id, filename=path.name,
            content=path.read_bytes(), display_name=label,
        )

    def _assert_catalog(self, record, filename, segments):
        shots, creative, _ = package_script_catalog(
            record["local_path"], expected_category_id=record["category_id"],
        )
        expected_names = [name for name, count in segments for _ in range(count)]
        self.assertEqual([shot.structure for shot in shots], expected_names)
        self.assertEqual(
            [(shot.source_file, shot.source_sheet, shot.source_row) for shot in shots],
            [(filename, "模版", row) for row in range(7, 7 + len(expected_names))],
        )
        self.assertEqual(
            [(shot.source_file, shot.source_sheet, shot.source_row) for shot in creative],
            [(filename, "创意库", row) for row in range(7, 10)],
        )
        self.assertEqual([shot.shot_no for shot in shots], [str(i) for i in range(1, len(shots) + 1)])
        package = load_asset_package(record["local_path"])
        references = [
            Path(package["package_root"]) / value.strip()
            for value in shots[0].reference_image_path.split(";") if value.strip()
        ]
        self.assertEqual(len(references), 2)
        self.assertTrue(all(path.is_file() for path in references))
        self.assertEqual(Path(package["source_workbook_path"]).name, filename)
        self.assertEqual(package["template_rule_contract"]["status"], "ready")
        return package

    def test_two_templates_keep_distinct_contiguous_structures_and_default_alias(self):
        first_path = _ready_template(self.root / "first", "匿名方案甲.xlsx")
        first = self._import(first_path, label="方案甲")
        self.assertEqual(self.registry.list()["aliases"], {})
        self.registry.promote("launch", first["package_id"], first["package_version"])
        before = self.registry.list()

        second_path = _ready_template(self.root / "second", "匿名方案乙.xlsx", SECOND_SEGMENTS)
        second = self._import(second_path, label="方案乙")

        self.assertNotEqual(first["package_id"], second["package_id"])
        self.assertEqual(self.registry.list()["aliases"], before["aliases"])
        self.assertEqual(self.registry.list()["alias_history"], before["alias_history"])
        self.assertEqual(self.registry.resolve_formal("launch"), first)
        for record, path, label, segments in (
            (first, first_path, "方案甲", FIRST_SEGMENTS),
            (second, second_path, "方案乙", SECOND_SEGMENTS),
        ):
            with self.subTest(label=label):
                self._assert_catalog(record, path.name, segments)
                item = self.service.describe_template(record)
                self.assertEqual(item["label"], label)
                self.assertEqual(item["source_name"], path.name)
                self.assertEqual(item["shot_count"], sum(count for _, count in segments))
                self.assertEqual(item["structures"], _expected_structures(segments))
                self.assertEqual(self.service.resolve_template("launch", item["ref"]), record)
        self.assertEqual(
            {record["package_id"] for record in self.service.template_records("launch")},
            {first["package_id"], second["package_id"]},
        )

    def test_repeated_import_is_idempotent_and_does_not_rewrite_registered_files(self):
        path = _ready_template(self.root / "source", "匿名可复用方案.xlsx")
        source_sha = sha256_file(path)
        first = self._import(path)
        registered_files = build_package_record(first["local_path"])
        registry_sha = sha256_file(self.registry.path)

        second = self._import(path)

        self.assertEqual(second, first)
        self.assertEqual(build_package_record(first["local_path"]), registered_files)
        self.assertEqual(sha256_file(self.registry.path), registry_sha)
        self.assertEqual(sha256_file(path), source_sha)
        self.assertEqual(len(self.service.template_records("launch")), 1)

    def test_ready_template_with_implicit_source_brand_remains_importable_after_upload(self):
        path = _ready_template(
            self.root / "original-folder", "匿名未填来源品牌方案.xlsx", source_brand="",
        )
        before = check_ai_spec(path)
        source_sha = sha256_file(path)

        record = self._import(path)

        self._assert_catalog(record, path.name, FIRST_SEGMENTS)
        package = load_asset_package(record["local_path"])
        after = check_ai_spec(package["source_workbook_path"])
        self.assertTrue(after.in_sync)
        self.assertTrue(after.usable)
        self.assertEqual(after.rows, before.rows)
        self.assertEqual(sha256_file(path), source_sha)

    def test_relocated_template_still_detects_human_visual_and_explicit_brand_edits(self):
        for field, source_brand, cell, value in (
            ("visual", "", "I7", "搬运后修改的画面描述"),
            ("brand", "匿名素材", "Q7", "另一匿名品牌"),
            ("brand_clear", "匿名素材", "Q7", None),
        ):
            with self.subTest(field=field):
                path = _ready_template(
                    self.root / field / "original", "匿名待同步方案.xlsx", source_brand=source_brand,
                )
                source_sha = sha256_file(path)
                moved = self.root / field / "uploaded" / path.name
                moved.parent.mkdir()
                moved.write_bytes(path.read_bytes())
                self.assertTrue(check_ai_spec(moved).in_sync)
                workbook = load_workbook(moved)
                try:
                    workbook["模版"][cell] = value
                    workbook.save(moved)
                finally:
                    workbook.close()

                report = check_ai_spec(moved)

                self.assertFalse(report.in_sync)
                self.assertFalse(report.usable)
                self.assertIn("ai_spec_out_of_sync", report.validation_codes)
                before_registry = self.registry.list()
                with self.assertRaises(PackagePublicationError) as error:
                    self._import(moved)
                self.assertEqual(error.exception.code, "AI_SPEC_NOT_READY")
                after_registry = self.registry.list()
                self.assertEqual(after_registry["packages"], before_registry["packages"])
                self.assertEqual(after_registry["aliases"], before_registry["aliases"])
                self.assertFalse(self.registry.path.exists())
                self.assertEqual(sha256_file(path), source_sha)

    def test_legacy_32_column_ai_spec_is_read_only_compatible_and_upgrade_keeps_source_hashes(self):
        for source_brand, expected_origin in (("匿名素材", "explicit"), ("", "inferred")):
            with self.subTest(origin=expected_origin):
                path = _ready_template(
                    self.root / expected_origin, "匿名旧规范方案.xlsx", source_brand=source_brand,
                )
                before = check_ai_spec(path)
                _remove_brand_origin_column(path)
                legacy_sha = sha256_file(path)

                legacy = check_ai_spec(path)

                self.assertTrue(legacy.in_sync)
                self.assertTrue(legacy.usable)
                self.assertEqual(sha256_file(path), legacy_sha)
                self.assertEqual([row.source_hash for row in legacy.rows], [row.source_hash for row in before.rows])
                self.assertEqual({row.source_brand_origin for row in legacy.rows}, {""})
                upgraded = refresh_ai_spec(path)
                self.assertTrue(upgraded.changed)
                self.assertTrue(upgraded.in_sync)
                self.assertTrue(upgraded.usable)
                self.assertEqual([row.source_hash for row in upgraded.rows], [row.source_hash for row in legacy.rows])
                self.assertEqual({row.source_brand_origin for row in upgraded.rows}, {expected_origin})

    def test_legacy_implicit_brand_requires_refresh_at_original_location_before_move(self):
        original = _ready_template(
            self.root / "original-legacy-folder", "匿名旧目录来源.xlsx", source_brand="",
        )
        _remove_brand_origin_column(original)
        self.assertTrue(check_ai_spec(original).in_sync)
        moved = self.root / "uploaded" / original.name
        moved.parent.mkdir()
        moved.write_bytes(original.read_bytes())
        moved_sha = sha256_file(moved)

        legacy_moved = check_ai_spec(moved)

        self.assertFalse(legacy_moved.in_sync)
        self.assertIn("ai_spec_out_of_sync", legacy_moved.validation_codes)
        self.assertEqual(sha256_file(moved), moved_sha)
        upgraded = refresh_ai_spec(original)
        moved.write_bytes(original.read_bytes())
        current_moved = check_ai_spec(moved)
        self.assertTrue(current_moved.in_sync)
        self.assertTrue(current_moved.usable)
        self.assertEqual(current_moved.rows, upgraded.rows)

    def test_v3_upgrade_preserves_imported_rules_and_original_source(self):
        path = _ready_template(self.root / "source", "匿名十镜方案.xlsx", SECOND_SEGMENTS)
        record = self._import(path)
        before = build_package_record(record["local_path"])
        source_package = load_asset_package(record["local_path"])
        rules_path = Path(source_package["package_root"]) / source_package["template_rules"]
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        self.assertNotEqual(rules, builtin_template_rules("launch"))

        upgraded_path = bundle_asset_package_v3(
            record["local_path"], self.root / "upgraded", package_version=2,
        )

        upgraded = build_package_record(upgraded_path)
        package = self._assert_catalog(upgraded, path.name, SECOND_SEGMENTS)
        upgraded_rules_path = Path(package["package_root"]) / package["template_rules"]
        self.assertEqual(upgraded_rules_path.read_bytes(), rules_path.read_bytes())
        self.assertEqual(package["template_rules_sha256"], source_package["template_rules_sha256"])
        self.assertEqual(sha256_file(package["source_workbook_path"]), sha256_file(source_package["source_workbook_path"]))
        self.assertEqual(build_package_record(record["local_path"]), before)

    def test_each_of_six_categories_imports_explicit_workbook_and_reads_catalog(self):
        for index, category_id in enumerate(CATEGORIES):
            with self.subTest(category_id=category_id):
                segments = (("场地建立", 2), ("主题呈现", 3 + index), ("收束", 3))
                path = _ready_template(self.root / category_id, f"匿名方案{index + 1}.xlsx", segments)
                record = self._import(path, category_id, label=f"匿名方案{index + 1}")
                self.assertEqual(record["category_id"], category_id)
                self._assert_catalog(record, path.name, segments)
                item = self.service.describe_template(record)
                self.assertEqual(item["shot_count"], 8 + index)
                self.assertEqual(item["structures"], _expected_structures(segments))
                listed = self.service.template_records(category_id)
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0], {key: value for key, value in record.items() if key != "resolved_ref"})
        self.assertEqual(self.registry.list()["aliases"], {})

    def test_each_category_keeps_project_image_library_bound_when_default_template_changes(self):
        defaults = FormalDefaultImageService(
            projects=None, tasks=None, delivery=None, artifacts=None, image_pool=None,
            image_workspace=None, asset_package_resolver=lambda _: None,
            writes_enabled=False, package_registry=self.registry,
        )
        for category_id in CATEGORIES:
            with self.subTest(category_id=category_id):
                first_file = _ready_template(self.root / category_id / 'first', '匿名已选模板.xlsx')
                second_file = _ready_template(self.root / category_id / 'second', '匿名新默认模板.xlsx', SECOND_SEGMENTS)
                first = self._import(first_file, category_id)
                second = self._import(second_file, category_id)
                self.registry.promote(category_id, second['package_id'], second['package_version'])
                for record, filename in ((first, first_file.name), (second, second_file.name)):
                    project = {'category_id': category_id,
                        'template_package_ref': record['resolved_ref'],
                        'template_package_sha256': record['package_sha256']}
                    frozen = {key: record[key] for key in ("package_id", "package_version", "package_sha256", "resolved_ref")}
                    package, media = defaults._package_media(project, {}, frozen)
                    self.assertEqual(package['package_id'], record['package_id'])
                    self.assertEqual(package['category_id'], category_id)
                    self.assertTrue(media)
                    sources = [link for item in media for link in item['source_links']]
                    self.assertTrue(sources)
                    self.assertEqual({link['source_file'] for link in sources}, {filename})
                    self.assertTrue(all(item['content_sha256'] and item['media_id'] for item in media))

    def test_small_template_keeps_category_creative_and_images_and_frozen_old_type(self):
        shared_path = _ready_template(self.root / "shared", "匿名共享创意库.xlsx")
        shared = self._import(shared_path)
        self.registry.promote("launch", shared["package_id"], shared["package_version"])
        small_path = _ready_template(self.root / "small", "匿名少图模板.xlsx", SECOND_SEGMENTS)
        workbook = load_workbook(small_path)
        for sheet in workbook:
            sheet._images = sheet._images[:1] if sheet.title == "模版" else []
        workbook.save(small_path)
        workbook.close()
        self.assertTrue(refresh_ai_spec(small_path).usable)
        small = self._import(small_path)
        summary = self.service.describe_template(small)["sources"]
        self.assertEqual(summary["creative_count"], 6)
        self.assertEqual(len(summary["libraries"]), 2)
        self.assertGreater(summary["image_count"], summary["libraries"][0]["image_count"])
        template, creative, _ = package_script_catalog(small["local_path"], expected_category_id="launch",
            library_paths=[shared["local_path"]])
        self.assertEqual(len(template), 10)
        self.assertEqual({shot.source_file for shot in creative}, {shared_path.name, small_path.name})
        defaults = FormalDefaultImageService(projects=None, tasks=None, delivery=None, artifacts=None,
            image_pool=None, image_workspace=None, asset_package_resolver=lambda _: None,
            writes_enabled=False, package_registry=self.registry)
        binding = {key: small[key] for key in ("package_id", "package_version", "package_sha256", "resolved_ref")}
        binding["library_bindings"] = [{key: shared[key] for key in
            ("package_id", "package_version", "package_sha256", "resolved_ref", "category_id")}]
        package, media = defaults._package_media({"category_id": "sports"}, {"脚本类型": "发布会"}, binding)
        self.assertEqual(package["category_id"], "launch")
        self.assertEqual(len(media), summary["image_count"])
        self.assertEqual(len({item["content_sha256"] for item in media}), len(media))
        self.assertEqual({source["package_id"] for item in media for source in item["package_sources"]},
                         {small["package_id"], shared["package_id"]})

    def test_stale_ai_spec_does_not_register_or_replace_existing_template(self):
        path = _ready_template(self.root / "source", "匿名待同步方案.xlsx")
        first = self._import(path)
        self.registry.promote("launch", first["package_id"], first["package_version"])
        before = self.registry.list()
        workbook = load_workbook(path)
        try:
            workbook["模版"]["I7"] = "人工新增的画面，尚未同步到 AI 规范表"
            workbook.save(path)
        finally:
            workbook.close()

        with self.assertRaises(PackagePublicationError) as error:
            self._import(path)

        self.assertEqual(error.exception.code, "AI_SPEC_NOT_READY")
        self.assertEqual(self.registry.list(), before)
        self.assertEqual(self.registry.resolve_formal("launch"), first)

    def test_template_reference_requires_exact_version_and_matching_category(self):
        path = _ready_template(self.root / "source", "匿名版本方案.xlsx")
        record = self._import(path)
        ref = self.service.describe_template(record)["ref"]
        for invalid in ("formal", record["package_id"], ref.rsplit("@", 1)[0] + "@latest"):
            with self.subTest(ref=invalid), self.assertRaises(PackagePublicationError) as error:
                self.service.resolve_template("launch", invalid)
            self.assertEqual(error.exception.code, "TEMPLATE_REF_INVALID")
        with self.assertRaises(PackagePublicationError) as error:
            self.service.resolve_template("sports", ref)
        self.assertEqual(error.exception.code, "TEMPLATE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
