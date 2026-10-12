from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from PIL import Image

from mvp_v2.asset_package import (
    AssetPackageError, bundle_asset_package_v2, create_asset_package,
    load_asset_package, media_source_provenance, script_payload_digest_manifest, search_asset_package,
    verify_script_payload_digest, update_asset_package_index,
)
from mvp_v2.excel_io import load_template_slots
from mvp_v2.run_contract import sha256_file, sha256_json


def write_multimage_source(path: Path):
    book = Workbook()
    sheet = book.active
    sheet.title = '模版'
    for col, label in [(1, '结构'), (2, '镜号')]:
        sheet.cell(5, col, label)
    for col, label in [(3, '画面示意'), (5, '效果示意'), (7, '画面描述'), (8, '声音')]:
        sheet.cell(6, col, label)
    sheet.merge_cells('C6:D6')
    sheet.merge_cells('E6:F6')
    for row in [7, 8, 9]:
        sheet.cell(row, 1, '开场造势')
        sheet.cell(row, 2, row - 6)
        sheet.cell(row, 7, f'匿名舞台灯光镜头 {row}')
        sheet.cell(row, 8, '音乐')
    # Independent oracle: four permitted references on row 7, one on row 8;
    # two effects; an unlabelled decoration at I7 must never be assigned.
    anchors = [('C7', 'red'), ('D7', 'blue'), ('D7', 'green'),
               ('C7', 'red'), ('C8', 'red'), ('E7', 'yellow'),
               ('F7', 'purple'), ('I7', 'orange')]
    for cell, color in anchors:
        stream = io.BytesIO()
        Image.new('RGB', (12, 9), color).save(stream, format='PNG')
        stream.seek(0)
        sheet.add_image(ExcelImage(stream), cell)
    book.save(path)
    book.close()
    return anchors


class RV03DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.workbook = self.source / 'M+发布会类通用脚本库.xlsx'
        self.anchors = write_multimage_source(self.workbook)

    def test_reading_frozen_workbook_extracts_outside_package_and_preserves_digest(self):
        from mvp_v2.package_registry import build_package_record
        editable = create_asset_package(self.root / 'editable', package_id='anonymous-frozen',
            source_root=self.source, source_workbook=self.workbook)
        manifest = bundle_asset_package_v2(editable, self.root / 'frozen', package_version=1)
        package = load_asset_package(manifest)
        before = build_package_record(manifest)
        cache_root = (self.root / 'runtime-cache').resolve()
        with patch('mvp_v2.runtime_config.load_runtime_config', return_value={'cache_root': str(cache_root)}):
            for _ in range(2):
                shots = load_template_slots(package['source_root_path'], workbook_path=package['source_workbook_path'])
                refs = [Path(value.strip()) for shot in shots for value in shot.reference_image_path.split(';') if value.strip()]
                self.assertGreaterEqual(len(refs), 5)
                for image in refs:
                    self.assertTrue(image.is_file())
                    self.assertTrue(image.is_relative_to(cache_root))
                    provenance = json.loads(image.with_suffix(
                        f'.{sha256_file(package["source_workbook_path"])}.provenance.json').read_text())
                    self.assertEqual(provenance['normalized_png_sha256'], sha256_file(image))
                    self.assertEqual(provenance['source_file'], self.workbook.name)
                self.assertEqual(build_package_record(manifest)['package_sha256'], before['package_sha256'])
        self.assertFalse(list(package['package_root'].rglob('_mvp_v2_extracted_images')))
        # Even a misplaced runtime cache must not write into the immutable package.
        fallback = (self.root / 'disposable-cache').resolve()
        with patch('mvp_v2.runtime_config.load_runtime_config',
                   return_value={'cache_root': str(package['package_root'] / 'cache')}), patch(
                   'tempfile.mkdtemp', return_value=str(fallback)):
            shots = load_template_slots(package['source_root_path'], workbook_path=package['source_workbook_path'])
            self.assertTrue(all(Path(value.strip()).is_relative_to(fallback)
                for shot in shots for value in shot.reference_image_path.split(';') if value.strip()))
        self.assertEqual(build_package_record(manifest)['package_sha256'], before['package_sha256'])


    def test_merged_ranges_same_cell_multiple_and_effect_boundaries(self):
        before = sha256_file(self.workbook)
        shots = load_template_slots(self.source)
        refs = [Path(v.strip()) for v in shots[0].reference_image_path.split(';')]
        effects = [Path(v.strip()) for v in shots[0].effect_reference_path.split(';')]
        self.assertEqual(len(refs), 4)
        self.assertEqual(len(effects), 2)
        positions = [json.loads(p.with_suffix(f'.{before}.provenance.json').read_text()) for p in refs + effects]
        self.assertEqual([(p['source_row'], p['source_column'], p['anchor_index']) for p in positions],
                         [(7, 3, 1), (7, 3, 4), (7, 4, 2), (7, 4, 3), (7, 5, 6), (7, 6, 7)])
        self.assertTrue(all(p['source_workbook_sha256'] == before for p in positions))
        self.assertEqual(sha256_file(refs[0]), positions[0]['normalized_png_sha256'])
        self.assertEqual(sha256_file(self.workbook), before)
        # Removing explicit range evidence must not guess the adjacent column.
        book = load_workbook(self.workbook)
        book['模版'].unmerge_cells('C6:D6')
        book.save(self.workbook)
        book.close()
        shots = load_template_slots(self.source)
        self.assertEqual(len(shots[0].reference_image_path.split(';')), 2)

    def test_media_dedup_retains_all_source_rows_and_anchor_locations(self):
        manifest = create_asset_package(self.root / 'index', package_id='anonymous', source_root=self.source)
        package = load_asset_package(manifest)
        with sqlite3.connect(package['index_path']) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_assets').fetchone()[0], 5)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_locations').fetchone()[0], 7)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_script_links').fetchone()[0], 6)
            multiple = db.execute('SELECT media_id FROM media_script_links GROUP BY media_id HAVING COUNT(*)=2').fetchone()[0]
            self.assertEqual(db.execute('SELECT source_row FROM media_script_links WHERE media_id=? ORDER BY source_row', (multiple,)).fetchall(), [(7,), (8,)])
            anchors = media_source_provenance(db, multiple, source_workbook_sha256=sha256_file(self.workbook))
            self.assertEqual([(a['source_row'], a['anchor_index']) for a in anchors], [(7, 1), (7, 4), (8, 5)])
        results = search_asset_package(manifest, '匿名舞台', kind='script')
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r['source_file'] == self.workbook.name for r in results))

    def test_export_keeps_reference_and_effect_in_separate_columns_without_duplicate_previews(self):
        from mvp_v2.excel_io import write_template_output_workbook
        source_digest = sha256_file(self.workbook)
        shots = load_template_slots(self.source)
        rows = [{'结构': shot.structure, '镜号': shot.shot_no,
                 '画面描述': shot.visual_desc, '声音': shot.sound, '时长': 2,
                 '画面示意': shot.reference_image_path,
                 '效果示意': shot.effect_reference_path} for shot in shots]
        output = self.root / 'anonymous-reference-effect.xlsx'
        write_template_output_workbook(output, rows, [], embed_review_previews=False)
        book = load_workbook(output)
        self.addCleanup(book.close)
        sheet = book['脚本']
        columns = {cell.value: cell.column - 1 for cell in sheet[1]}
        images = {(image.anchor._from.row, image.anchor._from.col): image
                  for image in sheet._images}
        # The existing export selects one preview per purpose and source row;
        # source multiplicity must not duplicate previews or replace an effect.
        expected = {(1, columns['画面示意']): 'red',
                    (1, columns['效果示意']): 'yellow',
                    (2, columns['画面示意']): 'red'}
        self.assertEqual(len(sheet._images), len(expected))
        self.assertEqual(set(images), set(expected))
        for anchor, color in expected.items():
            with Image.open(io.BytesIO(images[anchor]._data())) as rendered:
                self.assertEqual(rendered.convert('RGB').getpixel((0, 0)),
                                 Image.new('RGB', (1, 1), color).getpixel((0, 0)))
        self.assertEqual(sum(len(ws._images) for ws in book), len(expected))
        self.assertEqual(sha256_file(self.workbook), source_digest)

    def test_legacy_index_upgrade_preserves_analysis_and_manual_review_payload(self):
        manifest = create_asset_package(self.root / 'legacy-index',
            package_id='anonymous-legacy', source_root=self.source)
        index = load_asset_package(manifest)['index_path']
        raw_analysis = '{"caption":"旧人工画面说明","manual_review":{"actor":"anonymous-reviewer","note":"只确认现场来源，不代表品牌授权","status":"pending_review"},"legacy_extension":{"keep":[1,2]}}'
        with sqlite3.connect(index) as db:
            identities = db.execute('SELECT media_id, content_sha256 FROM media_assets ORDER BY media_id').fetchall()
            target = identities[0][0]
            db.execute("UPDATE media_assets SET analysis_json=?, caption=?, review_status=? WHERE media_id=?",
                (raw_analysis, '旧人工画面说明', 'pending_review', target))
            # A historical index only retained the first reference per source row.
            db.execute("DELETE FROM media_script_links WHERE rowid NOT IN "
                "(SELECT MIN(rowid) FROM media_script_links GROUP BY source_sheet, source_row, reference_kind)")
            for asset_id, raw in db.execute('SELECT asset_id, payload_json FROM script_assets').fetchall():
                payload = json.loads(raw)
                for field in ('reference_image_path', 'effect_reference_path'):
                    payload[field] = str(payload.get(field) or '').split(';')[0]
                db.execute('UPDATE script_assets SET payload_json=?, source_sha256=? WHERE asset_id=?',
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True), sha256_json(payload), asset_id))
            old_links = db.execute('SELECT COUNT(*) FROM media_script_links').fetchone()[0]
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        metadata = json.loads(manifest.read_text())
        metadata['index_schema_version'] = 'mplus-asset-index/v3'
        manifest.write_text(json.dumps(metadata, ensure_ascii=False))
        frozen = self.root / 'frozen-legacy-copy'
        shutil.copytree(manifest.parent, frozen)
        frozen_hashes = {str(path.relative_to(frozen)): sha256_file(path)
            for path in frozen.rglob('*') if path.is_file()}
        source_hash = sha256_file(self.workbook)
        update_asset_package_index(manifest)
        with sqlite3.connect(index) as db:
            self.assertEqual(db.execute('SELECT media_id, content_sha256 FROM media_assets ORDER BY media_id').fetchall(), identities)
            self.assertEqual(db.execute('SELECT analysis_json, caption, review_status FROM media_assets WHERE media_id=?',
                (target,)).fetchone(), (raw_analysis, '旧人工画面说明', 'pending_review'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_script_links').fetchone()[0], 6)
            self.assertLess(old_links, 6)
            paths = [json.loads(raw)['reference_image_path'] for raw, in db.execute(
                'SELECT payload_json FROM script_assets ORDER BY source_row')]
            self.assertEqual(len(paths[0].split(';')), 4)
        # Repeated upgrade/read must not replace the preserved human payload.
        update_asset_package_index(manifest)
        with sqlite3.connect(index) as db:
            self.assertEqual(db.execute('SELECT analysis_json FROM media_assets WHERE media_id=?',
                (target,)).fetchone()[0], raw_analysis)
        results = search_asset_package(manifest, '旧人工画面说明', kind='media')
        self.assertEqual([row['media_id'] for row in results], [target])
        self.assertEqual(sha256_file(self.workbook), source_hash)
        self.assertEqual({str(path.relative_to(frozen)): sha256_file(path)
            for path in frozen.rglob('*') if path.is_file()}, frozen_hashes)

    def test_payload_digest_definitions_tamper_legacy_and_directory_copy(self):
        source_manifest = create_asset_package(self.root / 'index', package_id='anonymous', source_root=self.source)
        source_index = load_asset_package(source_manifest)['index_path']
        with sqlite3.connect(source_index) as db:
            source_rows = db.execute('SELECT asset_id, source_sha256, payload_json FROM script_assets').fetchall()
        self.assertTrue(all(sha256_json(json.loads(raw)) == digest for _, digest, raw in source_rows))
        frozen_before = sha256_file(source_index)
        candidate = bundle_asset_package_v2(source_manifest, self.root / 'candidate')
        candidate_index = load_asset_package(candidate)['index_path']
        declared = load_asset_package(candidate)['script_payload_digests']
        self.assertTrue(all(row['packaged_payload_verified'] for row in search_asset_package(candidate, '', kind='script')))
        self.assertTrue(all(not row['packaged_payload_verified'] for row in search_asset_package(source_manifest, '', kind='script')))
        blobs = [json.loads(line) for line in (candidate.parent / 'blob_manifest.jsonl').read_text().splitlines()]
        self.assertEqual(sum(len(b['source_anchors']) for b in blobs), 7)
        with sqlite3.connect(candidate_index) as db:
            metadata = script_payload_digest_manifest(db)
            rows = db.execute('SELECT asset_id, source_sha256, payload_json FROM script_assets').fetchall()
        self.assertEqual(metadata, declared)
        for asset_id, digest, raw in rows:
            payload = json.loads(raw)
            self.assertTrue(verify_script_payload_digest(metadata, asset_id, payload, source_sha256=digest))
            self.assertFalse(verify_script_payload_digest(None, asset_id, payload, source_sha256=digest))
            with self.assertRaisesRegex(AssetPackageError, '封装内容摘要不匹配'):
                verify_script_payload_digest(metadata, asset_id, {**payload, 'visual_desc': 'changed'}, source_sha256=digest)
            with self.assertRaisesRegex(AssetPackageError, '来源摘要不匹配'):
                verify_script_payload_digest(metadata, asset_id, payload, source_sha256='0' * 64)
        self.assertTrue(any(metadata['records'][a]['packaged_payload_sha256'] != d for a, d, _ in rows))
        self.assertTrue(any(metadata['records'][a]['packaged_payload_sha256'] == d for a, d, _ in rows))
        shutil.copytree(candidate.parent, self.root / 'relocated')
        with sqlite3.connect(self.root / 'relocated' / 'asset_index.sqlite3') as db:
            self.assertEqual(script_payload_digest_manifest(db), metadata)
        self.assertEqual(sha256_file(source_index), frozen_before)
        relocated = self.root / 'relocated'
        self.assertTrue(all(r['packaged_payload_verified'] for r in search_asset_package(relocated, '', kind='script')))
        with sqlite3.connect(relocated / 'asset_index.sqlite3') as db:
            db.execute("UPDATE script_assets SET payload_json=json_set(payload_json, '$.visual_desc', '篡改') WHERE asset_id=?", (rows[0][0],))
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        with self.assertRaisesRegex(AssetPackageError, '封装内容摘要不匹配'):
            search_asset_package(relocated, '', kind='script')
        with self.assertRaisesRegex(AssetPackageError, '摘要缺失'):
            verify_script_payload_digest(metadata, 'missing', {}, source_sha256='')
        with self.assertRaisesRegex(AssetPackageError, '版本不受支持'):
            verify_script_payload_digest({}, 'missing', {}, source_sha256='')


if __name__ == '__main__':
    unittest.main()
