from __future__ import annotations

from copy import copy, deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
import shutil
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.styles import Font, PatternFill
from PIL import Image
from pypdf import PdfReader

from mvp_v2 import excel_io
from mvp_v2.audit import audit_output_workbook_format
from mvp_v2.formal_image_workspace import _replace_main_images, _verify_workbook
from mvp_v2.workspace_store import WorkspaceServiceError
from tests.test_mvp_v2_formal_generation_service import write_launch_library


class ParentWorkbookPreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mplus-parent-workbook-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _fixture(self, count):
        template = self.root / f'template-{count}.xlsx'
        raw = [['匿名结构', n, None, None, '切换', '地面', '全景', '固定',
                f'匿名第{n}镜现场', '轻音乐', '来源说明'] for n in range(1, count + 1)]
        write_launch_library(template, raw, [])
        scripts, details = [], []
        for n, row in enumerate(raw, 1):
            path = ''
            if n not in {1, count}:
                path = str(self.root / f'picture-{n}.png')
                Image.new('RGB', (96, 54), (n * 9, 90, n * 11)).save(path)
            scripts.append(dict(zip(['结构', '镜号', '画面示意', '效果示意', '效果', '类型',
                                     '景别', '运镜', '画面描述', '声音', '备注'], row)))
            scripts[-1].update({'镜号': str(n), '画面示意': path, '时长(秒)': 3})
            details.append({'项目名称': '匿名保真回归', 'slot_index': n, '模板镜号': str(n),
                            '模板来源文件': template.name, '模板来源 sheet': '模版', '模板来源行': n + 6,
                            '采用来源文件': template.name, '采用来源 sheet': '模版', '采用来源行': n + 6})
        parent = self.root / f'parent-{count}.xlsx'
        excel_io.write_template_output_workbook(parent, scripts, details, audit_rows=[],
                                                template_workbook_path=template,
                                                duration_seconds=count * 3)
        wb = load_workbook(parent)
        script = wb['脚本']
        script.freeze_panes = 'C7'
        script.page_setup.orientation = 'landscape'
        script.print_title_rows = '1:6'
        script.print_area = f'A1:K{count + 6}'
        script.oddFooter.center.text = '匿名制作 / 第 &P 页'
        script.column_dimensions['J'].width = 53
        for n in range(1, count + 1):
            script.cell(n + 6, 11).value = f'业务备注-{n}'
            script.cell(n + 6, 10).font = Font(name='Arial', size=12 + n % 3, color='163B52')
            script.row_dimensions[n + 6].height = 222 + n
        for picture in script._images:
            marker = picture.anchor._from
            picture.anchor = OneCellAnchor(_from=AnchorMarker(col=marker.col, row=marker.row,
                colOff=45000, rowOff=67000), ext=XDRPositiveSize2D(cx=850000, cy=470000))
        # A legacy adjacent table has no sound columns; the new evidence belongs on another page.
        adjacent = wb['相邻镜头复核']
        for column in sorted([cell.column for cell in adjacent[1]
                              if cell.value in {'前镜声音', '后镜声音'}], reverse=True):
            adjacent.delete_cols(column)
        adjacent.column_dimensions['C'].width = 49
        adjacent['C2'].fill = PatternFill('solid', fgColor='ACDDEE')
        note_column = adjacent.max_column + 1
        adjacent.cell(1, note_column, '业务补充')
        for n in range(1, count):
            adjacent.cell(n + 1, note_column, f'原第{n}组备注')
        editorial = wb['剪辑复核']
        editorial_headers = {cell.value: cell.column for cell in editorial[1]}
        editorial.cell(2, editorial_headers['总体审核'], '通过')
        editorial.cell(2, editorial_headers['审核人'], '旧版审核人')
        business = wb.create_sheet('制作备注')
        business.append(['事项', '说明', '统计'])
        business.append(['拍摄准备', '保留现场自然光', '=SUM(2,3)'])
        business['B2'].fill = PatternFill('solid', fgColor='FEDDBB')
        business.column_dimensions['B'].width = 41
        wb.save(parent)
        wb.close()
        return template, parent, scripts, details

    def _legacy_copy(self, omitted):
        template, parent, scripts, details = self._fixture(8)
        workbook = load_workbook(parent)
        for name, labels in omitted.items():
            sheet = workbook[name]
            for column in sorted([cell.column for cell in sheet[1] if cell.value in labels], reverse=True):
                sheet.delete_cols(column)
        workbook.save(parent)
        workbook.close()
        child = self.root / 'legacy-copy.xlsx'
        excel_io.write_template_output_workbook(child, scripts, details,
            template_workbook_path=template, parent_workbook_path=parent)
        return child

    def test_single_row_template_preserves_content_and_child_row_alignment(self):
        from tests.test_mvp_v2_template_selection import _ready_template
        template = _ready_template(self.root / 'compact', '匿名单层表头.xlsx',
            (('场地建立', 2), ('互动体验', 3), ('收束', 1)))
        before = template.read_bytes()
        scripts = [dict(结构='匿名环节', 镜号=str(n), 画面描述=f'已确认画面{n}',
            声音=f'现场声音{n}', 类型='地面', 旁白=f'旁白{n}', 字幕=f'字幕{n}')
            for n in range(1, 7)]
        details = [dict(项目名称='匿名表头回归', 模板镜号=str(n),
            模板来源文件=template.name, 模板来源行=n + 6) for n in range(1, 7)]
        parent = self.root / 'compact-parent.xlsx'
        excel_io.write_template_output_workbook(parent, scripts, details, [],
            template_workbook_path=template)
        workbook = load_workbook(parent)
        rows = excel_io.read_managed_sheet_rows(workbook, '脚本')
        self.assertEqual(len(rows), 6)
        for index, row in enumerate(rows):
            for field in ('镜号', '画面描述', '声音', '类型', '旁白', '字幕'):
                self.assertEqual(row[field], scripts[index][field])
        self.assertEqual(workbook['脚本']['I5'].value, '画面描述')
        self.assertEqual(workbook['脚本']['I7'].value, '已确认画面1')
        workbook.close()
        checks = audit_output_workbook_format(parent, template_workbook_path=template)
        self.assertNotIn('template_layout_missing', [check['检查项'] for check in checks])
        original = parent.read_bytes()
        scripts[2]['声音'] = '第三镜修改后的声音'
        scripts[2]['字幕'] = ''
        child = self.root / 'compact-child.xlsx'
        excel_io.write_template_output_workbook(child, scripts, details, [],
            template_workbook_path=template, parent_workbook_path=parent)
        workbook = load_workbook(child)
        rows = excel_io.read_managed_sheet_rows(workbook, '脚本')
        self.assertEqual(len(rows), 6)
        self.assertEqual([row['镜号'] for row in rows], [str(n) for n in range(1, 7)])
        self.assertEqual([row['声音'] for row in rows], [row['声音'] for row in scripts])
        self.assertIn(rows[2]['字幕'], ('', None))
        self.assertEqual(workbook['脚本']['I7'].value, '已确认画面1')
        self.assertEqual(workbook['脚本']['I12'].value, '已确认画面6')
        workbook.close()
        self.assertEqual(parent.read_bytes(), original)
        self.assertEqual(template.read_bytes(), before)
        # A genuinely damaged title merge must still be rejected.
        workbook = load_workbook(parent)
        title_merge = next(str(area) for area in workbook['脚本'].merged_cells.ranges
            if area.min_row == 1)
        workbook['脚本'].unmerge_cells(title_merge)
        workbook.save(parent)
        workbook.close()
        checks = audit_output_workbook_format(parent, template_workbook_path=template)
        self.assertIn('template_layout_missing', [check['检查项'] for check in checks])

    def test_saved_script_widths_are_the_revision_audit_baseline(self):
        template, parent, scripts, details = self._fixture(10)
        workbook = load_workbook(parent)
        workbook['脚本'].column_dimensions['C'].width = 112
        workbook['脚本'].column_dimensions['I'].width = 42
        workbook.save(parent)
        workbook.close()
        original = parent.read_bytes()
        child = self.root / 'image-child.xlsx'
        excel_io.write_template_output_workbook(
            child, scripts, details, template_workbook_path=template,
            parent_workbook_path=parent, duration_seconds=30,
        )
        checks = audit_output_workbook_format(
            child, expected_image_count=8, template_workbook_path=parent,
            duration_seconds=30,
        )
        self.assertFalse([check for check in checks if check['级别'] == 'hard'], checks)
        self.assertEqual(parent.read_bytes(), original)
        workbook = load_workbook(child)
        self.assertEqual(workbook['脚本'].column_dimensions['I'].width, 42)
        self.assertEqual(len(workbook['脚本']._images), 8)
        workbook['脚本'].column_dimensions['I'].width = 41
        workbook.save(child)
        workbook.close()
        checks = audit_output_workbook_format(
            child, expected_image_count=8, template_workbook_path=parent,
            duration_seconds=30,
        )
        self.assertIn('template_column_width_mismatch',
                      [check['检查项'] for check in checks if check['级别'] == 'hard'])

    @staticmethod
    def _images(sheet):
        result = []
        for picture in sheet._images:
            marker, extent = picture.anchor._from, picture.anchor.ext
            content = picture._data()
            picture.ref = BytesIO(content)
            with Image.open(BytesIO(content)) as pixels:
                result.append((marker.row, marker.col, marker.rowOff, marker.colOff, extent.cx, extent.cy,
                               sha256(pixels.convert('RGBA').tobytes()).hexdigest()))
        return result

    def test_sound_child_preserves_entire_parent_and_reads_new_evidence_separately(self):
        for count in (8, 12, 17):
            with self.subTest(count=count):
                template, parent, scripts, details = self._fixture(count)
                original = parent.read_bytes()
                before = load_workbook(BytesIO(original))
                scripts[2]['声音'] = '掌声收束后保留轻音乐'
                scripts[2]['字幕'] = '匿名字幕'
                child = self.root / f'child-{count}.xlsx'
                excel_io.write_template_output_workbook(child, scripts, details, audit_rows=[],
                    template_workbook_path=template, parent_workbook_path=parent,
                    parent_shot_nos=[str(n) for n in range(1, count + 1)], duration_seconds=count * 3)
                after = load_workbook(child)
                self.assertEqual(parent.read_bytes(), original)
                with ZipFile(parent) as original_zip, ZipFile(child) as child_zip:
                    self.assertEqual(sum(name.startswith('xl/media/') for name in child_zip.namelist()),
                                     sum(name.startswith('xl/media/') for name in original_zip.namelist()))
                self.assertEqual(after.sheetnames, [*before.sheetnames, '版本附加明细'])
                for name in before.sheetnames:
                    self.assertEqual(after[name].max_column, before[name].max_column, name)
                for name in ('脚本', '相邻镜头复核'):
                    self.assertEqual([c.value for c in after[name][6 if name == '脚本' else 1]],
                                     [c.value for c in before[name][6 if name == '脚本' else 1]])
                self.assertEqual(after['脚本']['J9'].value, scripts[2]['声音'])
                self.assertEqual(copy(after['脚本']['J9'].font), copy(before['脚本']['J9'].font))
                self.assertEqual(after['脚本'].column_dimensions['J'].width, 53)
                self.assertEqual(after['脚本'].row_dimensions[9].height, 225)
                self.assertEqual(after['脚本'].print_title_rows, before['脚本'].print_title_rows)
                self.assertEqual(str(after['脚本'].print_area), str(before['脚本'].print_area))
                self.assertEqual(after['脚本'].oddFooter.center.text, before['脚本'].oddFooter.center.text)
                self.assertEqual(self._images(after['脚本']), self._images(before['脚本']))
                self.assertEqual(after['制作备注']['C2'].value, '=SUM(2,3)')
                self.assertEqual(copy(after['制作备注']['B2'].fill), copy(before['制作备注']['B2'].fill))
                adjacent = excel_io.read_managed_sheet_rows(after, '相邻镜头复核')
                self.assertEqual(len(adjacent), count - 1)
                self.assertEqual(adjacent[1]['后镜声音'], scripts[2]['声音'])
                self.assertEqual(adjacent[2]['前镜声音'], scripts[2]['声音'])
                self.assertEqual(adjacent[2]['业务补充'], '原第3组备注')
                self.assertEqual(excel_io.read_managed_sheet_rows(after, '脚本')[2]['字幕'], '匿名字幕')
                review = excel_io.read_managed_sheet_rows(after, '剪辑复核')
                self.assertFalse(review[0]['总体审核'])
                self.assertFalse(review[0]['审核人'])
                before.close()
                after.close()
                # The audit pass renders from the same immutable parent, never the first output.
                second = self.root / f'child-audited-{count}.xlsx'
                excel_io.write_template_output_workbook(second, scripts, details, audit_rows=[],
                    template_workbook_path=template, parent_workbook_path=parent,
                    parent_shot_nos=[str(n) for n in range(1, count + 1)], duration_seconds=count * 3)
                a, b = load_workbook(child), load_workbook(second)
                self.assertEqual(a.sheetnames, b.sheetnames)
                self.assertEqual(list(a['版本附加明细'].values), list(b['版本附加明细'].values))
                a.close()
                b.close()

    def test_parent_without_template_preserves_large_source_image_pixels(self):
        _, parent, scripts, details = self._fixture(8)
        picture = self.root / 'large-original.png'
        Image.new('RGB', (512, 288), (120, 75, 211)).save(picture)
        workbook = load_workbook(parent)
        sheet = workbook['脚本']
        for index, image in enumerate(sheet._images):
            if image.anchor._from.row == 7:
                replacement = ExcelImage(picture)
                replacement.anchor = deepcopy(image.anchor)
                sheet._images[index] = replacement
                break
        workbook.save(parent)
        workbook.close()
        scripts[1]['画面示意'] = str(picture)
        before = load_workbook(parent)
        original_images = self._images(before['脚本'])
        before.close()
        output = self.root / 'without-template.xlsx'
        excel_io.write_template_output_workbook(output, scripts, details, parent_workbook_path=parent)
        after = load_workbook(output)
        self.assertEqual(self._images(after['脚本']), original_images)
        self.assertIn('匿名保真回归', after['脚本']['A1'].value)
        after.close()

    def test_deleted_and_moved_shots_keep_parent_image_geometry_by_explicit_mapping(self):
        template, parent, scripts, details = self._fixture(8)
        selected = [8, 3, 2, 4, 6, 7]
        child_scripts, child_details = [], []
        for n, old in enumerate(selected, 1):
            row, detail = deepcopy(scripts[old - 1]), deepcopy(details[old - 1])
            row['镜号'], detail['slot_index'] = str(n), n
            child_scripts.append(row)
            child_details.append(detail)
        output = self.root / 'reordered.xlsx'
        excel_io.write_template_output_workbook(output, child_scripts, child_details, audit_rows=[],
            template_workbook_path=template, parent_workbook_path=parent,
            parent_shot_nos=list(map(str, selected)), duration_seconds=18)
        wb = load_workbook(output)
        self.assertEqual(wb['脚本'].max_row, 12)
        self.assertTrue({'A5:A6', 'B5:B6'} <= set(map(str, wb['脚本'].merged_cells.ranges)))
        self.assertEqual(wb['脚本']['I6'].value, '画面描述')
        self.assertEqual(wb['相邻镜头复核'].max_row, 6)
        self.assertEqual([r['镜号'] for r in excel_io.read_managed_sheet_rows(wb, '脚本')],
                         [str(n) for n in range(1, 7)])
        self.assertEqual([wb['脚本'].row_dimensions[n + 6].height for n in range(1, 7)],
                         [222 + old for old in selected])
        self.assertEqual([x[0] for x in self._images(wb['脚本'])], [7, 8, 9, 10, 11])
        self.assertTrue(all(x[2:6] == (67000, 45000, 850000, 470000)
                            for x in self._images(wb['脚本'])))
        adjacent = excel_io.read_managed_sheet_rows(wb, '相邻镜头复核')
        self.assertFalse(adjacent[0]['业务补充'])
        self.assertEqual(adjacent[-1]['业务补充'], '原第6组备注')
        wb.close()

    def test_preserved_package_uses_default_content_type_namespace_for_independent_readers(self):
        template, parent, scripts, details = self._fixture(8)
        output = self.root / 'independent-reader-child.xlsx'
        scripts[2]['声音'] = '保持图片和来源的声音修改'
        excel_io.write_template_output_workbook(output, scripts, details, audit_rows=[],
            template_workbook_path=template, parent_workbook_path=parent)
        with ZipFile(output) as archive:
            manifest = archive.read('[Content_Types].xml')
            # System.IO.Packaging rejects a prefixed Types root before reading
            # any sheet. Keep the OPC envelope in its interoperable form.
            self.assertRegex(manifest.decode(), r'<Types\s+xmlns="http://schemas.openxmlformats.org/package/2006/content-types"')
            parsed = ET.fromstring(manifest)
            overrides = {entry.get('PartName') for entry in parsed if entry.get('PartName')}
            self.assertIn('/xl/workbook.xml', overrides)
            self.assertTrue(any(part.startswith('/xl/drawings/') for part in overrides))
        reopened = load_workbook(output)
        self.assertEqual(len(reopened['脚本']._images), 6)
        self.assertEqual(reopened['脚本']['J9'].value, '保持图片和来源的声音修改')
        reopened.close()

    def test_opaque_zip_members_and_ole_relationship_survive_managed_update(self):
        template, parent, scripts, details = self._fixture(8)
        with ZipFile(parent) as archive:
            entries = {item.filename: archive.read(item.filename) for item in archive.infolist()}
        namespace = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        relationships = 'http://schemas.openxmlformats.org/package/2006/relationships'
        docrels = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        sheet = ET.fromstring(entries['xl/worksheets/sheet1.xml'])
        objects = ET.SubElement(sheet, f'{{{namespace}}}oleObjects')
        ET.SubElement(objects, f'{{{namespace}}}oleObject', {'progId': 'Package', 'shapeId': '7',
                                                          f'{{{docrels}}}id': 'rIdOpaque'})
        entries['xl/worksheets/sheet1.xml'] = ET.tostring(sheet)
        rels_name = 'xl/worksheets/_rels/sheet1.xml.rels'
        rels = ET.fromstring(entries[rels_name])
        ET.SubElement(rels, f'{{{relationships}}}Relationship', {'Id': 'rIdOpaque',
            'Type': docrels + '/oleObject', 'Target': '../embeddings/oleObject1.bin'})
        entries[rels_name] = ET.tostring(rels)
        entries['xl/embeddings/oleObject1.bin'] = b'anonymous-opaque-embedded-document'
        entries['customXml/anonymous.xml'] = b'<anonymous>unchanged</anonymous>'
        business_xml = ET.fromstring(entries['xl/worksheets/sheet7.xml'])
        business_xml.find(f'.//{{{namespace}}}c[@r="C2"]/{{{namespace}}}v').text = '5'
        entries['xl/worksheets/sheet7.xml'] = ET.tostring(business_xml)
        package_rels = ET.fromstring(entries['_rels/.rels'])
        ET.SubElement(package_rels, f'{{{relationships}}}Relationship', {'Id': 'rIdCustom',
            'Type': docrels + '/customXml', 'Target': 'customXml/anonymous.xml'})
        entries['_rels/.rels'] = ET.tostring(package_rels)
        with ZipFile(parent, 'w', ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        scripts[2]['声音'] = '新声音'
        output = self.root / 'ole-child.xlsx'
        excel_io.write_template_output_workbook(output, scripts, details, audit_rows=[],
            template_workbook_path=template, parent_workbook_path=parent)
        with ZipFile(output) as archive:
            self.assertEqual(archive.read('xl/embeddings/oleObject1.bin'), entries['xl/embeddings/oleObject1.bin'])
            self.assertEqual(archive.read('customXml/anonymous.xml'), entries['customXml/anonymous.xml'])
            self.assertEqual(archive.read('xl/worksheets/sheet7.xml'), entries['xl/worksheets/sheet7.xml'])
            self.assertIn(b'oleObject', archive.read('xl/worksheets/sheet1.xml'))
            self.assertIn(b'oleObject1.bin', archive.read(rels_name))
            self.assertIn(b'customXml/anonymous.xml', archive.read('_rels/.rels'))
        reopened = load_workbook(output)
        self.assertEqual(reopened['脚本']['J9'].value, '新声音')
        reopened.close()
        excel_io.scrub_workbook_paths(output, set())
        exported = excel_io.build_script_review_workbook(output, version_no=2)
        with ZipFile(BytesIO(exported)) as archive:
            self.assertEqual(archive.read('xl/embeddings/oleObject1.bin'), entries['xl/embeddings/oleObject1.bin'])
            self.assertIn(b'oleObject1.bin', archive.read(rels_name))
            self.assertEqual(archive.read('xl/worksheets/sheet7.xml'), entries['xl/worksheets/sheet7.xml'])
        cached = load_workbook(BytesIO(exported), data_only=True)
        self.assertEqual(cached['制作备注']['C2'].value, 5)
        cached.close()

    def test_confirmed_image_replacement_preserves_other_images_and_rejects_missing_or_duplicate_main(self):
        _, parent, scripts, _ = self._fixture(8)
        workbook = load_workbook(parent)
        path = self.root / 'confirmed.png'
        Image.new('RGB', (96, 54), (23, 113, 194)).save(path)
        workbook['脚本'].add_image(ExcelImage(path), 'D7')
        workbook['脚本'].add_image(ExcelImage(path), 'E2')
        workbook.save(parent)
        workbook.close()
        child = self.root / 'confirmed-child.xlsx'
        child.write_bytes(parent.read_bytes())
        paths = {str(n): path for n in range(1, 9)}
        decisions = [{'image_sha256': sha256(path.read_bytes()).hexdigest()} for _ in range(8)]
        _replace_main_images(child, scripts, paths)
        excel_io.scrub_workbook_paths(child, set(paths.values()))
        _verify_workbook(child, decisions, paths)
        workbook = load_workbook(child)
        pictures = self._images(workbook['脚本'])
        self.assertIn((6, 3), [item[:2] for item in pictures])
        self.assertIn((1, 4), [item[:2] for item in pictures])
        main = [item for item in pictures if item[1] == 2]
        self.assertEqual(len(main), 8)
        self.assertTrue(all(item[2:6] == (67000, 45000, 850000, 470000)
                            for item in main if 7 <= item[0] < 13))
        workbook['脚本']._images = [picture for picture in workbook['脚本']._images
                                   if (picture.anchor._from.row, picture.anchor._from.col) != (6, 2)]
        missing = self.root / 'missing-main.xlsx'
        workbook.save(missing)
        workbook.close()
        with self.assertRaises(WorkspaceServiceError):
            _verify_workbook(missing, decisions, paths)
        workbook = load_workbook(child)
        workbook['脚本'].add_image(ExcelImage(path), 'C7')
        duplicate = self.root / 'duplicate-main.xlsx'
        workbook.save(duplicate)
        workbook.close()
        with self.assertRaises(WorkspaceServiceError):
            _verify_workbook(duplicate, decisions, paths)

    def test_review_and_delivery_read_missing_review_columns_from_additions_without_private_values(self):
        template, parent, scripts, details = self._fixture(8)
        workbook = load_workbook(parent)
        for name in ('剪辑复核', '整片复核'):
            sheet = workbook[name]
            for column in sorted([cell.column for cell in sheet[1]
                                  if cell.value in {'审核人', '审核日期'}], reverse=True):
                sheet.delete_cols(column)
        detail_sheet = workbook['抽调明细']
        for column in sorted([cell.column for cell in detail_sheet[1]
                              if cell.value in {'Brief来源 SHA256', 'Brief来源证据'}], reverse=True):
            detail_sheet.delete_cols(column)
        workbook.save(parent)
        workbook.close()
        scripts[2]['字幕'] = '可读字幕'
        details[0]['Brief来源 SHA256'] = '7' * 64
        details[0]['Brief来源证据'] = '/private/anonymous/brief.txt'
        child = self.root / 'review-child.xlsx'
        excel_io.write_template_output_workbook(child, scripts, details,
            template_workbook_path=template, parent_workbook_path=parent)
        review = load_workbook(BytesIO(excel_io.build_script_review_workbook(child, version_no=2)))
        self.assertEqual(excel_io.read_managed_sheet_rows(review, '脚本')[2]['字幕'], '可读字幕')
        for row in review['版本附加明细']:
            for cell in row:
                self.assertNotIn('/private/', str(cell.value))
                self.assertNotIn('7' * 64, str(cell.value))
        review.close()
        model = {'issues': [{'issue_key': f'image-{n}', 'shot_no': str(n), 'kind': 'image',
                             'status': 'approve'} for n in range(1, 9)],
                 'decisions': [{'issue_key': f'image-{n}', 'decision': 'approve',
                                'reviewer_label': '匿名审核人', 'decided_at': '2026-09-09'}
                               for n in range(1, 9)],
                 'final_confirmation': {'decision': 'quality_pass', 'reviewer_label': '匿名审核人',
                                        'confirmed_at': '2026-09-09'}}
        delivered = self.root / 'delivery.xlsx'
        excel_io.write_reviewed_delivery_workbook(child, delivered, review_model=model)
        workbook = load_workbook(delivered)
        rows = excel_io.read_managed_sheet_rows(workbook, '剪辑复核')
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row['总体审核'] == '通过' and row['审核人'] == '匿名审核人' for row in rows))
        self.assertEqual(excel_io.read_managed_sheet_rows(workbook, '整片复核')[0]['审核人'], '匿名审核人')
        self.assertNotIn('审核人', [cell.value for cell in workbook['剪辑复核'][1]])
        workbook.close()

    def test_legacy_quality_revision_and_publish_readers_use_version_additions(self):
        from mvp_v2.audit import _workbook_project_name
        from mvp_v2.auto_pipeline import _workbook_metrics
        from mvp_v2.review_publish_stages import _workbook_contract_counts, _invalid_review_dates
        from mvp_v2.review_quality import analyze_review_workbooks
        from mvp_v2.revision import extract_revision_constraints

        template, parent, scripts, details = self._fixture(8)
        workbook = load_workbook(parent)
        omitted = {
            '抽调明细': {'项目名称', '采用来源文件', '采用来源 sheet', '采用来源行', '时长(秒)'},
            '剪辑复核': {'声音', '总体审核', '修改意见', '审核人', '审核日期'},
        }
        for name, labels in omitted.items():
            sheet = workbook[name]
            for column in sorted([cell.column for cell in sheet[1] if cell.value in labels], reverse=True):
                sheet.delete_cols(column)
        workbook.save(parent)
        workbook.close()
        for row in details:
            row['时长(秒)'] = 3
        output = self.root / 'legacy-reader-child.xlsx'
        excel_io.write_template_output_workbook(output, scripts, details,
            template_workbook_path=template, parent_workbook_path=parent)
        counts = _workbook_contract_counts(output)
        self.assertEqual(counts['shot_count'], 8)
        self.assertEqual(counts['adjacent_edge_count'], 7)
        self.assertEqual(counts['source_triplet_count'], 8)
        metrics = _workbook_metrics(output, {})
        self.assertEqual(metrics['total_duration_seconds'], 24)
        self.assertEqual(metrics['source_triplet_count'], 8)
        # The quality reader must retain pending review, not treat missing physical columns as passed.
        project_quality = analyze_review_workbooks([output])['projects'][0]
        self.assertEqual(project_quality['total_shot_count'], 8)
        self.assertEqual(project_quality['status'], 'pending_review')
        workbook = load_workbook(output)
        self.assertEqual(_workbook_project_name(workbook), '匿名保真回归')
        excel_io._set_managed_row_values(workbook, '剪辑复核', 3, {
            '声音': '掌声淡出后保留轻音乐', '总体审核': '需修改', '修改意见': '请修改第三镜声音',
            '审核人': '匿名审核人', '审核日期': 'not-a-date',
        })
        workbook.save(output)
        workbook.close()
        constraints = extract_revision_constraints(output)
        self.assertEqual(len(constraints), 1)
        self.assertEqual(constraints[0]['shot_no'], '3')
        self.assertIn('掌声淡出后保留轻音乐', constraints[0]['desired_change'])
        self.assertEqual(constraints[0]['reviewer'], '匿名审核人')
        self.assertEqual(_invalid_review_dates(output), ['剪辑复核!审核日期4 无效'])

    def test_review_addition_values_are_editable_but_identity_and_style_are_immutable(self):
        from mvp_v2.review_package import _immutable_workbook_sha256

        source = self._legacy_copy({'剪辑复核': {'总体审核', '审核人'}})
        original = _immutable_workbook_sha256(source)
        workbook = load_workbook(source)
        excel_io._set_managed_row_values(workbook, '剪辑复核', 2, {'总体审核': '通过', '审核人': '匿名审核人'})
        changed = self.root / 'reviewed-additions.xlsx'
        workbook.save(changed)
        workbook.close()
        self.assertEqual(_immutable_workbook_sha256(changed), original)
        for column, value in ((1, '脚本'), (2, 3), (3, '画面描述'), (5, 'unexpected')):
            with self.subTest(column=column):
                workbook = load_workbook(changed)
                row = next(row for row in workbook['版本附加明细'].iter_rows(min_row=2)
                           if row[0].value == '剪辑复核' and row[1].value == 2 and row[2].value == '总体审核')
                workbook['版本附加明细'].cell(row[0].row, column, value)
                altered = self.root / f'altered-{column}.xlsx'
                workbook.save(altered)
                workbook.close()
                self.assertNotEqual(_immutable_workbook_sha256(altered), original)
        workbook = load_workbook(changed)
        workbook['版本附加明细']['D2'].fill = PatternFill('solid', fgColor='FF0000')
        styled = self.root / 'altered-style.xlsx'
        workbook.save(styled)
        workbook.close()
        self.assertNotEqual(_immutable_workbook_sha256(styled), original)

    def test_review_project_names_can_be_verified_from_additions(self):
        from mvp_v2.review_package import _validate_review_project_names, ReviewPackageError

        source = self._legacy_copy({name: {'项目名称'} for name in ('剪辑复核', '相邻镜头复核', '整片复核')})
        _validate_review_project_names(source, '匿名保真回归')
        with self.assertRaises(ReviewPackageError):
            _validate_review_project_names(source, '另一个项目')

    def test_review_package_export_preserves_opaque_parts_and_accepts_reviewed_additions(self):
        from mvp_v2.review_package import export_review_workbook, validate_review_workbook
        from tests.test_mvp_v2_acceptance import _published_release

        source = self._legacy_copy({'剪辑复核': {'总体审核', '审核人'}})
        with ZipFile(source, 'a', ZIP_DEFLATED) as archive:
            archive.writestr('customXml/anonymous.xml', b'<anonymous>preserve</anonymous>')
        def copied_workbook(path, *args, **kwargs):
            shutil.copyfile(source, path)
        with patch('tests.test_mvp_v2_acceptance.write_template_output_workbook', side_effect=copied_workbook):
            run = _published_release(self.root, '匿名保真回归')
        output = export_review_workbook(run, self.root / 'exported-review.xlsx')
        with ZipFile(output) as archive:
            self.assertEqual(archive.read('customXml/anonymous.xml'), b'<anonymous>preserve</anonymous>')
        workbook = load_workbook(output)
        excel_io._set_managed_row_values(workbook, '剪辑复核', 2, {'总体审核': '通过', '审核人': '匿名审核人'})
        workbook.save(output)
        workbook.close()
        self.assertEqual(validate_review_workbook(run, output)['source_run_id'], run.name)

    def test_visual_review_reads_evidence_on_additions_page(self):
        from mvp_v2.visual_review import _review_rows

        workbook = Workbook()
        workbook.active.title = '配图复核'
        workbook['配图复核'].append(['镜号', '配图判断'])
        workbook['配图复核'].append(['2', '匹配'])
        excel_io._set_managed_row_values(workbook, '配图复核', 1, {'视觉内容摘要': '匿名展厅全景'})
        self.assertEqual(_review_rows(workbook)[0]['视觉内容摘要'], '匿名展厅全景')
        workbook.close()

    def test_saved_main_image_reader_handles_short_moved_columns_and_blank_first_shot(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = '脚本'
        sheet.append(['效果示意', '镜号', '画面描述', '画面示意'])
        expected = {}
        for number, color in enumerate(('red', 'blue', 'green'), 1):
            sheet.append(['', str(number), '匿名现场', ''])
            picture = self.root / f'reader-{number}.png'
            Image.new('RGB', (90, 60), color).save(picture)
            sheet.add_image(ExcelImage(picture), f'A{number + 1}')
            if number > 1:
                sheet.add_image(ExcelImage(picture), f'D{number + 1}')
                expected[str(number)] = picture.read_bytes()
        sheet.add_image(ExcelImage(picture), 'D40')
        output = self.root / 'short-moved.xlsx'
        workbook.save(output)
        workbook.close()
        before = output.read_bytes()
        self.assertEqual(excel_io.read_script_image_bytes(output), expected)
        self.assertEqual(output.read_bytes(), before)

    def test_formal_pdf_uses_main_picture_even_when_effect_picture_is_last(self):
        from mvp_v2.v10_image_delivery import create_formal_snapshot_pdf, V10ImageDeliveryError

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = '脚本'
        sheet.append(['镜号', '画面描述', '声音', '画面示意', '效果示意'])
        scripts, traces = [], []
        for number, color in enumerate(('red', 'blue'), 1):
            script = {'镜号': str(number), '画面描述': '匿名现场', '声音': '现场声音'}
            scripts.append(script)
            sheet.append([str(number), '匿名现场', '现场声音'])
            main = self.root / f'pdf-main-{number}.png'
            effect = self.root / f'pdf-effect-{number}.png'
            Image.new('RGB', (90, 60), color).save(main)
            Image.new('RGB', (90, 60), 'green').save(effect)
            sheet.add_image(ExcelImage(main), f'D{number + 1}')
            sheet.add_image(ExcelImage(effect), f'E{number + 1}')
            traces.append({'shot_no': str(number), 'source_file': 'anonymous.xlsx', 'source_sheet': '模版',
                           'source_row': number + 6, 'image_sha256': sha256(main.read_bytes()).hexdigest()})
        source = self.root / 'pdf-source.xlsx'
        workbook.save(source)
        workbook.close()
        def create(path, output):
            return create_formal_snapshot_pdf(path, workbook_sha256=sha256(path.read_bytes()).hexdigest(),
                snapshot_digest='a' * 64, project_id='anonymous-project', version_id='anonymous-v2',
                run_id='anonymous-run', source_trace=traces, script_rows=scripts, output_path=output)
        result = create(source, self.root / 'with-effect.pdf')
        pdf = PdfReader(result.pdf_path)
        self.assertEqual(pdf.pages[0].images[0].image.convert('RGB').getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(pdf.pages[1].images[0].image.convert('RGB').getpixel((0, 0)), (0, 0, 255))
        workbook = load_workbook(source)
        workbook['脚本']._images = [picture for picture in workbook['脚本']._images
                                   if (picture.anchor._from.row, picture.anchor._from.col) != (1, 3)]
        missing = self.root / 'pdf-missing-main.xlsx'
        workbook.save(missing)
        workbook.close()
        with self.assertRaises(V10ImageDeliveryError):
            create(missing, self.root / 'missing-main.pdf')
