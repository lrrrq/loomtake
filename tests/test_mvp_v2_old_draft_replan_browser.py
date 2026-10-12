"""RV05-02 existing frozen version and dirty progress survive new Brief adoption."""
import unittest
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect
from mvp_v2 import local_ui
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from tests import test_mvp_v2_formal_image_workspace as image_fixture
from tests import test_mvp_v2_formal_generation_service as generation_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture
from tests.test_mvp_v2_library_browse import library_confirmation_agent


def seed_confirmed_source(f):
    # The old document must genuinely contain the facts later re-extracted.
    adapter = FormalBriefInputAdapter(f.briefs, data_root=f.root/'runtime')
    content = "\n".join((
        '项目名称: 匿名发布会', '脚本类型: 发布会', '品牌或客户: 匿名品牌',
        '目标时长: 60秒', '风格调性: 克制、真实', '视频目标: 产品演示',
        '使用场景: 线下新品发布现场', '必须出现的内容: 核心宣讲与仪式',
        '关键事件与流程: 开场造势、核心宣讲与仪式、收束',
        '禁止或不能出现的内容: 不得虚构事实', '参考图片: 已确认的现场主视觉参考'))
    adapter.import_uploads(f.token, workspace_id=f.workspace_id,
        project_id=f.project['project_id'], expected_project_version=0,
        uploads=[UploadedDocument('anonymous-existing-brief.txt', BytesIO(content.encode()))])
    base = f.briefs.latest_import_base(f.token, workspace_id=f.workspace_id,
        project_id=f.project['project_id'])
    return f.briefs.confirm(f.token, workspace_id=f.workspace_id,
        project_id=f.project['project_id'], brief_version_id=base.brief_version_id,
        expected_project_version=1, confirmed_at='2026-09-20T00:00:00Z')


class OldDraftReplanBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_new_brief_old_text_and_images_discard_then_replan_and_refresh(self):
        self._replan_existing_draft(change_type=False)

    def test_replan_committed_response_loss_refresh_recovers_one_child(self):
        self._replan_existing_draft(change_type=False, lose_generation_response=True)

    def test_changed_type_old_draft_discard_replan_preserves_frozen_parent(self):
        self._replan_existing_draft(change_type=True)

    def test_compact_template_recovery_download_preserves_generated_contents(self):
        self._replan_existing_draft(change_type=True, compact_template=True)

    def test_same_type_template_replacement_preserves_brief_and_frozen_parent(self):
        self._replan_existing_draft(change_type=False, compact_template=True, replace_template=True)

    def _replan_existing_draft(self, *, change_type, compact_template=False, replace_template=False, lose_generation_response=False):
        use_template = change_type or replace_template
        category = 'music_festival' if change_type else 'launch'
        template_label = '匿名音乐节模板' if change_type else '匿名新发布会模板'

        f = image_fixture.FormalImageWorkspaceTests('runTest')
        with patch.object(generation_fixture.FormalGenerationServiceTests, "_confirmed_brief", seed_confirmed_source):
            f.setUp()
        self.addCleanup(f.doCleanups)
        if use_template:
            from mvp_v2.asset_storage import LocalAssetStorage
            from mvp_v2.package_publication import AssetPackagePublicationService
            from mvp_v2.package_registry import LocalPackageRegistry
            registry = LocalPackageRegistry(f.base.generated.root / "recovery-template-registry.json")
            publication = AssetPackagePublicationService(
                storage=LocalAssetStorage(f.base.generated.root / "recovery-template-objects"),
                registry=registry, packages_root=f.base.generated.root / "recovery-template-packages")
            if compact_template:
                from tests.test_mvp_v2_template_selection import _ready_template
                template = _ready_template(f.base.generated.root / 'compact-template',
                    template_label + '.xlsx', (('场地建立', 2), ('嘉宾入场', 2),
                    ('核心宣讲与仪式', 4), ('互动体验', 3), ('收束', 1)))
            else:
                # Use the full renderable fixture, not the catalog-only compact template.
                template = f.base.generated.root / "匿名音乐节模板.xlsx"
                template.write_bytes((f.base.generated.source_root / "M+发布会类通用脚本库.xlsx").read_bytes())
                from openpyxl import load_workbook
                from tests.test_mvp_v2_ai_spec import HEADERS
                workbook = load_workbook(template)
                sheet = workbook['模版']
                for column, header in enumerate(HEADERS[11:], 12):
                    sheet.cell(6, column, header)
                for row in range(7, sheet.max_row + 1):
                    sheet.cell(row, 12, '可复用')
                    sheet.cell(row, 14, '活动展示')
                    sheet.cell(row, 15, '工作人员')
                    sheet.cell(row, 16, '活动;现场')
                    sheet.cell(row, 17, '匿名素材')
                    sheet.cell(row, 9, '匿名活动现场：工作人员展示已确认活动环节')
                workbook.save(template)
                workbook.close()
                from mvp_v2.ai_spec import refresh_ai_spec
                report = refresh_ai_spec(template)
                self.assertTrue(report.usable, report)
            record = publication.import_template(category_id=category,
                filename=template.name, content=template.read_bytes())
            registry.promote(category, record["package_id"], record["package_version"])
            f.base.generation.template_packages = publication
        scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        script = f.base.workspace
        current = script.read_current(f.token, **scope)
        old_version = current['base_version_id']
        old_context = f.delivery.resolve(f.token, f.project_id, old_version)
        old_paths = [f.resolver.resolve_stage_artifact(old_context.run_id, stage).path
            for stage in ('script_plan', 'script_draft', 'visual_binding')]
        old_bytes = [path.read_bytes() for path in old_paths]
        changed = script.apply_table_change(f.token, **scope,
            expected_project_version=current['project_version'], expected_revision=current['revision'],
            shot_no='1', shot_id=current['shots'][0]['shot_id'], field='subtitle',
            value='旧需求下尚未保存的字幕', idempotency_key='old-draft-before-brief')
        f.workspace.save_progress(f.token, **scope, base_version_id=old_version,
            expected_project_version=current['project_version'], expected_revision=0,
            expected_script_revision=changed['revision'], idempotency_key='old-image-before-brief',
            selections=[dict(shot_id=current['shots'][0]['shot_id'], image_key='', checked=False,
                decision_kind='temporary_text_basis', temporary_reason='旧稿待补图', reuse_reason='', query='')])
        agent = library_confirmation_agent(root=f.base.generated.root, store=f.store, projects=f.projects, service=None)
        app = local_ui.create_app(agent_api=agent, formal_script_service=script,
            formal_image_service=f.workspace, formal_generation_service=f.base.generation,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.base.generated.root/'runtime'), data_root=f.base.generated.root/'runtime'),
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + f'/projects/{f.project_id}/brief')
        expect(page.locator('#formal-standard-card-state')).to_have_text('已确认', timeout=60000)
        page.wait_for_load_state('networkidle')
        if not replace_template:
            if change_type:
                page.locator('#formal-brief-category-edit').click(timeout=60000)
                page.locator('#formal-project-category').select_option('music_festival')
                page.locator('#formal-brief-category-apply').click()
            else:
                page.locator('[data-edit-formal-fact="风格调性"]').click(timeout=60000)
                page.locator('#formal-brief-text').fill('风格调性: 高级')
            page.locator('#formal-brief-upload-button').click()
            expect(page.locator('#formal-standard-card-state')).to_have_text('待确认', timeout=60000)
            conflict = page.locator('#formal-conflict-list .ui00-conflict-item').filter(has=page.locator('select'))
            if conflict.count():
                expect(conflict).to_have_count(1)
                conflict.locator('select').select_option(label='音乐节' if change_type else '高级')
                conflict.get_by_role('button', name='确认选择', exact=True).click()
                expect(page.locator('#formal-conflict-list select')).to_have_count(0)
            page.locator('#formal-brief-confirm').click()
            expect(page.locator('#formal-standard-card-state')).to_have_text('已确认', timeout=60000)
        if use_template:
            page.locator('#formal-template-picker > summary').click()
            expect(page.locator('#formal-template-current')).to_contain_text(template_label)
            page.locator('#formal-template-confirm').check()
            with page.expect_response(lambda r: r.url.endswith('/templates/select')) as selected:
                page.locator('#formal-template-apply').click()
            self.assertEqual(selected.value.status, 409, selected.value.text())
            self.assertEqual(selected.value.json()['detail']['code'], 'SCRIPT_DRAFT_NOT_SAVED')
        new_brief = f.projects.get_project(f.token, f.project_id)['current_brief_snapshot_id']
        old_brief = json.loads(old_bytes[0])['confirmed_brief_input']['brief_snapshot_id']
        if replace_template:
            self.assertEqual(new_brief, old_brief)
        else:
            self.assertNotEqual(new_brief, old_brief)
        page.goto(origin + f'/projects/{f.project_id}/script')
        if not replace_template:
            expect(page.locator('#formal-script-basis-notice')).to_be_visible(timeout=60000)
        self.assertEqual(script.read_current(f.token, **scope)['has_new_confirmed_brief'], not replace_template)
        page.locator('[data-creative-edit-mode="table"]').click()
        expect(page.locator('[data-script-shot="1"][data-script-field="subtitle"]')).to_have_value('旧需求下尚未保存的字幕')
        page.locator('[data-creative-panel="images"]').click()
        expect(page.locator('[data-image-shot]')).to_have_count(len(current['shots']))
        page.once('dialog', lambda dialog: dialog.accept())
        with page.expect_response(lambda r: '/progress/discard?' in r.url and r.request.method=='POST', timeout=60000) as discarded:
            page.locator('#formal-script-discard').click()
        self.assertEqual(discarded.value.status, 200, discarded.value.text())
        expect(page.locator('#toast')).to_contain_text('全部草稿修改已放弃', timeout=60000)
        expect(page.locator('#formal-script-discard')).to_be_disabled()
        self.assertFalse(script.read_current(f.token, **scope)['can_save_version'])
        progress = f.workspace.read_progress(f.token, **scope)
        self.assertFalse(any(item['selection_active'] for item in progress['selections']))
        if use_template:
            page.goto(origin + f'/projects/{f.project_id}/brief?panel=templates')
            expect(page.locator('#formal-template-current')).to_contain_text(template_label, timeout=60000)
            page.locator('#formal-template-confirm').check()
            with page.expect_response(lambda r: r.url.endswith('/templates/select')) as selected:
                page.locator('#formal-template-apply').click()
            self.assertEqual(selected.value.status, 200, selected.value.text())
            self.assertEqual(selected.value.json()['selected_ref'], record['resolved_ref'])
        page.goto(origin + f'/projects/{f.project_id}/script?panel=structure&mode=text-only')
        with page.expect_response(lambda r: r.request.method=='POST' and '/plans?' in r.url, timeout=60000) as planned:
            page.locator('#formal-plan-create').click(timeout=60000)
        self.assertEqual(planned.value.status, 200, planned.value.text())
        plan = planned.value.json()
        try:
            page.locator('#formal-plan-confirm-check').check()
        except Exception:
            print('Recovery plan:', json.dumps(plan, ensure_ascii=False))
            print('Recovery page:', page.locator('body').inner_text())
            raise
        confirm_calls = []
        if lose_generation_response:
            def lose_response(route):
                confirm_calls.append(route.request.post_data_json)
                response = route.fetch(timeout=60000)
                self.assertEqual(response.status, 200, response.text())
                committed.append(response.json())
                route.abort('connectionreset')
            committed = []
            page.route('**/plans/*/confirm?*', lose_response)
            page.locator('#formal-plan-confirm').click()
            expect(page.locator('#formal-plan-confirm')).to_be_enabled(timeout=60000)
            self.assertEqual(len(committed), 1)
            child = committed[0]['version_id']
        else:
            with page.expect_response(lambda r: r.request.method=='POST' and '/plans/' in r.url and '/confirm' in r.url, timeout=60000) as generated:
                page.locator('#formal-plan-confirm').click()
            if generated.value.status != 200:
                from openpyxl import load_workbook
                for output in (f.base.generated.root / 'runs').rglob('*.xlsx'):
                    wb = load_workbook(output, read_only=True)
                    print('Output diagnostic:', output.name, wb.sheetnames,
                        [(ws.title, ws['A1'].value) for ws in wb])
                    wb.close()
                for audit in (f.base.generated.root / 'runs').rglob('*audit*.json'):
                    print('Audit diagnostic:', audit.name, audit.read_text()[:12000])
            self.assertEqual(generated.value.status, 200, generated.value.text())
            child = generated.value.json()['version_id']
        self.assertNotEqual(child, old_version)
        page.reload()
        persisted = script.read_current(f.token, **scope)
        self.assertEqual(persisted['base_version_id'], child)
        self.assertFalse(persisted['has_new_confirmed_brief'])
        if lose_generation_response:
            page.goto(origin + f'/projects/{f.project_id}/script')
            expect(page.locator('#creative-next-label')).to_be_visible(timeout=60000)
            page.reload()
            expect(page.locator('#creative-next-label')).to_be_visible(timeout=60000)
            self.assertEqual(len(confirm_calls), 1)
            self.assertEqual(script.read_current(f.token, **scope)['base_version_id'], child)
        if compact_template:
            from openpyxl import load_workbook
            from mvp_v2.excel_io import read_managed_sheet_rows
            page.goto(origin + f'/projects/{f.project_id}/script')
            page.locator('.creative-more > summary').click(timeout=60000)
            with page.expect_download(timeout=60000) as download:
                page.locator('#formal-script-review-workbook').click(timeout=60000)
            self.assertIsNone(download.value.failure())
            workbook = load_workbook(BytesIO(Path(download.value.path()).read_bytes()))
            rows = read_managed_sheet_rows(workbook, '脚本')
            self.assertEqual(len(rows), len(persisted['shots']))
            for row, shot in zip(rows, persisted['shots']):
                self.assertEqual(str(row['镜号']), str(shot['shot_no']))
                self.assertEqual(row['画面描述'], shot['visual'])
                self.assertEqual(row['声音'], shot['sound'])
                self.assertTrue(row['画面描述'])
            workbook.close()

        expect(page.locator('#formal-script-basis-notice')).to_be_hidden(timeout=60000)
        self.assertEqual([path.read_bytes() for path in old_paths], old_bytes)
        with f.store.connection() as connection:
            parent = connection.execute('SELECT parent_version_id FROM project_versions WHERE version_id=?',(child,)).fetchone()[0]
            self.assertEqual(parent, old_version)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM project_versions WHERE project_id=?',(f.project_id,)).fetchone()[0],2)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM version_quality_final_confirmations').fetchone()[0],0)
        self.assertEqual(f.projects.get_project(f.token, f.project_id)['current_brief_snapshot_id'], new_brief)
        self.assertTrue(plan['plan_id'])
        child_context = f.delivery.resolve(f.token, f.project_id, child)
        child_plan = json.loads(f.resolver.resolve_stage_artifact(child_context.run_id, 'script_plan').path.read_text())
        self.assertEqual(child_plan['confirmed_brief_input']['brief_snapshot_id'], new_brief)
        self.assertIn('音乐节' if change_type else ('发布会' if replace_template else '高级'),
            json.dumps(child_plan['brief'], ensure_ascii=False))
        if replace_template:
            self.assertEqual(f.projects.get_project(f.token, f.project_id)['category_id'], 'launch')
            self.assertEqual(len(persisted['shots']), 12)
            self.assertNotEqual(len(persisted['shots']), len(current['shots']))
            self.assertEqual(plan['template_summary']['label'], template_label)
            self.assertEqual(child_plan['confirmed_brief_input'], json.loads(old_bytes[0])['confirmed_brief_input'])

        if change_type:
            self.assertEqual(f.projects.get_project(f.token, f.project_id)['category_id'], 'music_festival')
            self.assertIn('发布会', json.dumps(json.loads(old_bytes[0])['brief'], ensure_ascii=False))


