from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook

from mvp_v2.ai_spec import check_ai_spec, load_ai_spec_shots, refresh_ai_spec
from mvp_v2.asset_package import create_asset_package, bundle_asset_package_v3, load_asset_package, package_script_catalog
from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.package_publication import AssetPackagePublicationService
from mvp_v2.package_registry import LocalPackageRegistry, build_package_record
from mvp_v2.template_rules import rules_from_template_shots
from mvp_v2.excel_io import _read_launch_workbook
from tests.test_mvp_v2_ai_spec import _write_library, _human_snapshot


def semantic_library(root: Path) -> Path:
    path = _write_library(root, template_count=3, creative_count=2, with_images=True)
    workbook = load_workbook(path)
    for sheet in (workbook['模版'], workbook['创意库']):
        sheet['A2'] = '整体说明'
        sheet['B2'] = '约60秒；自然光。若夜场可使用屏幕光，不要求增加镜头。'
        sheet['T5'] = '通用描述'
        sheet['T7'] = '交代活动规模、场地与主题'
        sheet['S7'] = '含案例品牌标识，不得直接作为客户素材。'
        sheet.merge_cells('A7:A9' if sheet.title == '模版' else 'A7:A8')
    workbook.save(path)
    workbook.close()
    return path


class SourceSemanticsTests(unittest.TestCase):
    def test_candidate_binds_real_sources_preserves_images_and_leaves_old_version_and_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = root / 'sources'
            sources.mkdir()
            path = semantic_library(sources)
            refresh_ai_spec(path)
            template, _ = load_ai_spec_shots(sources)
            rules = rules_from_template_shots('launch', template)
            for rule in rules['rules']:
                rule['selector']['source_sheet'] = '模版库'
            seed = create_asset_package(root / 'seed', package_id='launch-core', source_root=sources)
            base = bundle_asset_package_v3(seed, root / 'versions' / 'v1', template_rules=rules)
            registry = LocalPackageRegistry(root / 'registry.json')
            registry.register(base)
            registry.promote('launch', 'launch-core', 1)
            service = AssetPackagePublicationService(storage=LocalAssetStorage(root / 'storage'),
                registry=registry, packages_root=root / 'versions')
            original = build_package_record(base)
            before, _, _ = package_script_catalog(base, expected_category_id='launch')
            candidate = service.create_semantic_revision(category_id='launch', source_ref='launch-core@1')
            self.assertEqual(candidate['package_version'], 2)
            self.assertEqual(registry.resolve_formal('launch')['package_version'], 1)
            self.assertEqual(build_package_record(base)['package_sha256'], original['package_sha256'])
            package = load_asset_package(candidate['local_path'])
            after, _, source = package_script_catalog(candidate['local_path'], expected_category_id='launch')
            self.assertEqual([s.visual_desc for s in after], [s.visual_desc for s in before])
            self.assertEqual(len(after[0].reference_image_path.split(';')), 2)
            self.assertTrue(check_ai_spec(package['source_workbook_path']).in_sync)
            self.assertEqual(source['source_semantics']['status'], 'recorded')
            self.assertIn('若夜场', source['source_semantics']['sheets'][0]['overview_cells'][0]['text'])
            selector = package['template_rule_contract']['payload']['rules'][0]['selector']
            self.assertEqual(selector['source_sheet'], '模版')
            self.assertEqual([row['source_row'] for row in selector['sources']], [7, 8, 9])
            self.assertEqual(service.create_semantic_revision(category_id='launch', source_ref='launch-core@1')['package_sha256'], candidate['package_sha256'])

    def test_projection_preserves_intent_case_brand_and_exact_anchors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = semantic_library(root)
            before = _human_snapshot(path)
            report = refresh_ai_spec(path)
            self.assertTrue(report.usable)
            row = report.rows[0]
            self.assertEqual(row.generic_desc, '交代活动规模、场地与主题')
            self.assertEqual(row.visual_desc, '模板画面1')
            self.assertEqual(row.brand_judgement_original, '含案例品牌标识，不得直接作为客户素材。')
            self.assertFalse(row.brand_neutral)
            self.assertEqual(row.brand_neutral_evidence, '')
            self.assertIn('若夜场', row.sheet_overview)
            anchors = json.loads(row.semantic_source_anchors)
            self.assertEqual(anchors['generic_desc'], ['T7'])
            self.assertEqual(anchors['sheet_overview'], ['B2'])
            self.assertEqual(_human_snapshot(path), before)
            human = _read_launch_workbook(path, '模版')
            ai, creative = load_ai_spec_shots(root)
            self.assertEqual([s.generic_desc for s in ai], [s.generic_desc for s in human])
            self.assertEqual(len(ai), 3)
            self.assertEqual(len(creative), 2)
            self.assertEqual(ai[1].structure, ai[0].structure)
            self.assertEqual(len(ai[0].reference_image_path.split('; ')), 2)
            self.assertIn('交代活动规模', ai[0].searchable_text())

    def test_overview_change_invalidates_new_spec_without_rewriting_original_cells(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = semantic_library(Path(temporary))
            initial = refresh_ai_spec(path)
            original_hash = initial.rows[0].source_hash
            workbook = load_workbook(path)
            workbook['模版']['B2'] = '约30秒；夜场。保留前提和例外。'
            workbook.save(path)
            workbook.close()
            self.assertFalse(check_ai_spec(path).in_sync)
            refreshed = refresh_ai_spec(path)
            self.assertNotEqual(refreshed.rows[0].source_hash, original_hash)
            self.assertEqual(refreshed.rows[0].sheet_overview, '约30秒；夜场。保留前提和例外。')


if __name__ == '__main__':
    unittest.main()
