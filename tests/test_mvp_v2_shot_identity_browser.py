"""RV05-01 real UI deletion, subsequent edit and image-page refresh."""
import unittest
from unittest.mock import patch
from mvp_v2 import local_ui
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from tests.test_mvp_v2_library_browse import library_confirmation_agent
from playwright.sync_api import expect
from tests import test_mvp_v2_script_save_browser as fixture


class ShotIdentityBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ScriptSaveBrowserTests.setUpClass.__func__)
    setUp = fixture.ScriptSaveBrowserTests.setUp
    def start_server(self):
        agent = library_confirmation_agent(root=self.root, store=self.store, projects=self.base.projects, service=None)
        agent.script_revision = fixture.FormalScriptRevisionService(projects=self.base.projects,
            tasks=fixture.TaskService(self.store), workspace=self.script, image_workspace=self.images, writes_enabled=True)
        self.app = local_ui.create_app(agent_api=agent, formal_script_service=self.script, formal_image_service=self.images,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(self.store,
                artifact_root=self.root / 'runtime'), data_root=self.root / 'runtime'),
            formal_brief_upload_factory=uploaded_document)
        fixture.ScriptSaveBrowserTests.start_server(self)
        allowed = patch.object(local_ui, 'ALLOWED_ORIGINS',
            local_ui.ALLOWED_ORIGINS | {f'http://127.0.0.1:{self.port}'})
        allowed.start()
        self.addCleanup(allowed.stop)
    new_page = fixture.ScriptSaveBrowserTests.new_page
    current = fixture.ScriptSaveBrowserTests.current
    cell = fixture.ScriptSaveBrowserTests.cell

    def test_discard_lost_text_response_reload_continues_original_scope(self):
        original_version = self.current()['base_version_id']
        calls = []
        def lose_response(route):
            calls.append(route.request.post_data_json)
            response = route.fetch()
            self.assertEqual(response.status, 200, response.text())
            route.abort('connectionreset')
        self.page.route('**/script/discard?*', lose_response)
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.locator('#formal-script-discard').click()
        expect(self.page.locator('#formal-script-save-state')).to_contain_text('再次点击可继续原请求')
        self.assertFalse(self.current()['can_save_version'])
        self.page.reload()
        expect(self.page.locator('#formal-script-discard')).to_be_enabled()
        self.page.locator('#formal-script-discard').click()
        expect(self.page.locator('#toast')).to_contain_text('全部草稿修改已放弃')
        expect(self.page.locator('#formal-script-discard')).to_be_disabled()
        self.assertEqual(len(calls), 1)
        self.page.reload()
        expect(self.page.locator('#formal-script-discard')).to_be_disabled()
        self.assertFalse(self.current()['can_save_version'])
        self.assertEqual(self.current()['base_version_id'], original_version)
        self.assertEqual(self.errors, [])

    def test_discard_lost_image_response_replays_same_request_after_refresh(self):
        model = self.current()
        progress = self.images.read_progress(self.token, **self.scope)
        self.images.save_progress(self.token, **self.scope, base_version_id=model['base_version_id'],
            expected_project_version=model['project_version'], expected_revision=progress['revision'],
            expected_script_revision=model['revision'], idempotency_key='discard-image-draft',
            selections=[dict(shot_id=model['shots'][0]['shot_id'], image_key='', checked=False,
                decision_kind='temporary_text_basis', temporary_reason='匿名缺图进度待恢复',
                reuse_reason='', query='')])
        self.page.reload()
        calls = []
        def lose_first_response(route):
            calls.append(route.request.post_data_json)
            response = route.fetch()
            self.assertEqual(response.status, 200, response.text())
            if len(calls) == 1:
                route.abort('connectionreset')
            else:
                route.fulfill(response=response)
        self.page.route('**/image-workspace/progress/discard?*', lose_first_response)
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.locator('#formal-script-discard').click()
        expect(self.page.locator('#formal-script-save-state')).to_contain_text('再次点击可继续原请求')
        self.page.reload()
        expect(self.page.locator('#formal-script-discard')).to_be_enabled()
        self.page.locator('#formal-script-discard').click()
        expect(self.page.locator('#toast')).to_contain_text('全部草稿修改已放弃')
        expect(self.page.locator('#formal-script-discard')).to_be_disabled()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.page.reload()
        expect(self.page.locator('#formal-script-discard')).to_be_disabled()
        self.assertFalse(self.current()['can_save_version'])
        self.assertEqual(self.current()['base_version_id'], model['base_version_id'])
        self.assertEqual(self.images.read_progress(self.token, **self.scope)['selections'], [])
        self.assertEqual(self.errors, [])

    def test_discard_revision_conflict_preserves_new_edit_and_allows_fresh_confirmation(self):
        original_version = self.current()['base_version_id']
        calls = []
        def concurrent_edit(route):
            calls.append(route.request.post_data_json)
            model = self.current()
            self.script.apply_table_change(self.token, **self.scope,
                expected_project_version=model['project_version'], expected_revision=model['revision'],
                shot_no='2', shot_id=model['shots'][1]['shot_id'], field='subtitle',
                value='并发窗口保留的字幕', idempotency_key='discard-concurrent-edit')
            route.continue_()
        self.page.route('**/script/discard?*', concurrent_edit)
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.locator('#formal-script-discard').click()
        expect(self.page.locator('#formal-script-save-state')).to_contain_text('重新核对放弃范围')
        self.assertEqual(self.current()['shots'][1]['subtitle'], '并发窗口保留的字幕')
        self.assertTrue(self.current()['can_save_version'])
        self.page.unroute('**/script/discard?*', concurrent_edit)
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.locator('#formal-script-discard').click()
        expect(self.page.locator('#toast')).to_contain_text('全部草稿修改已放弃')
        expect(self.page.locator('#formal-script-discard')).to_be_disabled()
        self.page.reload()
        expect(self.page.locator('#formal-script-discard')).to_be_disabled()
        self.assertFalse(self.current()['can_save_version'])
        self.assertEqual(self.current()['base_version_id'], original_version)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.errors, [])

    def test_consecutive_deletions_undo_redo_keep_protection_on_original_entity(self):
        original = self.current()
        protected = original['shots'][2]
        self.page.locator('#creative-protection-details > summary').click()
        self.page.locator('#formal-script-lock-edit').click()
        self.page.locator(f'[data-script-lock-shot="{protected["shot_id"]}"]').check()
        with self.page.expect_response(lambda r: '/script/assistant-locks?' in r.url and r.request.method == 'PUT') as locked:
            self.page.locator('#formal-script-lock-save').click()
        self.assertEqual(locked.value.status, 200, locked.value.text())
        self._delete_edit_images_refresh()
        def assert_protection(expected):
            current = self.current()
            self.assertEqual([s['shot_id'] for s in current['shots']], [s['shot_id'] for s in expected])
            self.assertEqual([s['source'] for s in current['shots']], [s['source'] for s in expected])
            self.assertEqual([s['shot_id'] for s in current['shots'] if s['assistant_locked']], [protected['shot_id']])
            return current
        assert_protection(original['shots'][1:])
        if not self.page.locator('#creative-assistant').evaluate('(el) => el.open'):
            self.page.locator('#creative-assistant > summary').click()
        self.page.locator('#creative-assistant-scope').select_option('all')
        self.page.locator('#formal-script-instruction').fill('删除第1镜')
        self.page.locator('#formal-script-instruction-submit').click()
        with self.page.expect_response(lambda r: r.request.method == 'POST' and '/confirm' in r.url) as deleted:
            self.page.locator('[data-confirm-script-revision]').click()
        self.assertEqual(deleted.value.status, 200, deleted.value.text())
        assert_protection(original['shots'][2:])
        for action, expected in [('undo', original['shots'][1:]), ('redo', original['shots'][2:])]:
            with self.page.expect_response(lambda r: r.request.method == 'POST' and f'/script/{action}?' in r.url) as history:
                self.page.locator('#formal-script-' + action).click()
            self.assertEqual(history.value.status, 200, history.value.text())
            assert_protection(expected)
        self.page.reload()
        expect(self.page.locator('#creative-shot-count')).to_have_text(f"{len(original['shots']) - 2} 镜")
        current = assert_protection(original['shots'][2:])
        self.assertEqual(current['shots'][0]['shot_id'], protected['shot_id'])
        self.assertEqual(current['base_version_id'], original['base_version_id'])
        self.assertEqual(self.errors, [])

    def test_delete_edit_images_refresh_preserves_survivor(self):
        self._delete_edit_images_refresh()

    def test_delete_undo_redo_save_child_refresh_keeps_survivor_identity(self):
        original = self.current()
        old_context = self.base.delivery.resolve(self.token, self.project_id, original['base_version_id'])
        old_paths = [self.base.resolver.resolve_stage_artifact(old_context.run_id, stage).path
            for stage in ('script_plan', 'script_draft', 'visual_binding')]
        old_bytes = [path.read_bytes() for path in old_paths]
        self._delete_edit_images_refresh()
        edited = self.current()
        # Undo the edit and then deletion; redo both without rebuilding identities.
        for action, expected in [('undo', original['shots'][1:]),
                                 ('undo', original['shots']),
                                 ('redo', original['shots'][1:]),
                                 ('redo', edited['shots'])]:
            with self.page.expect_response(lambda r: r.request.method == 'POST'
                    and f'/script/{action}?' in r.url) as history:
                self.page.locator('#formal-script-' + action).click()
            self.assertEqual(history.value.status, 200, history.value.text())
            current = self.current()
            self.assertEqual([item['shot_id'] for item in current['shots']],
                [item['shot_id'] for item in expected])
            self.assertEqual([item['subtitle'] for item in current['shots']],
                [item['subtitle'] for item in expected])
            expect(self.page.locator('#creative-shot-count')).to_have_text(f"{len(expected)} 镜")
        self.page.locator('#creative-save > summary').click()
        self.page.locator('#formal-script-save-reason').fill('删除首镜、恢复历史后保存幸存镜修改')
        with self.page.expect_response(lambda r: r.request.method == 'POST'
                and '/script/save?' in r.url, timeout=60000) as proposal:
            self.page.locator('#formal-script-save').click()
        self.assertEqual(proposal.value.status, 200, proposal.value.text())
        task_id = proposal.value.json()['task_id']
        with self.page.expect_response(lambda r: r.request.method == 'POST'
                and f'/tasks/{task_id}/confirm' in r.url, timeout=60000) as saved:
            self.page.locator('[data-confirm-script-revision]').click()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        self.assertEqual(saved.value.json()['task']['status'], 'completed', saved.value.text())
        expect(self.page.locator('#formal-script-save-proposal')).to_contain_text('已保存为一个新版本', timeout=60000)
        self.page.reload()
        self.page.locator('[data-creative-edit-mode="table"]').click()
        persisted = self.current()
        self.assertNotEqual(persisted['base_version_id'], original['base_version_id'])
        self.assertFalse(persisted['can_save_version'])
        self.assertEqual([s['shot_id'] for s in persisted['shots']], [s['shot_id'] for s in edited['shots']])
        self.assertEqual([s['source'] for s in persisted['shots']], [s['source'] for s in edited['shots']])
        self.assertEqual(persisted['shots'][0]['subtitle'], '删除后幸存镜字幕')
        expect(self.page.locator(f'[data-script-shot-key="{edited["shots"][0]["shot_id"]}"][data-script-field="subtitle"]')).to_have_value('删除后幸存镜字幕')
        self.page.locator('[data-creative-panel="images"]').click()
        expect(self.page.locator('[data-image-shot]')).to_have_count(len(edited['shots']))
        self.assertEqual([path.read_bytes() for path in old_paths], old_bytes)
        with self.store.connection() as connection:
            rows = connection.execute('SELECT version_id, parent_version_id FROM project_versions WHERE project_id=?',
                (self.project_id,)).fetchall()
            self.assertEqual(len(rows), 2)
            child = next(row for row in rows if row['version_id'] == persisted['base_version_id'])
            self.assertEqual(child['parent_version_id'], original['base_version_id'])
        self.assertEqual(self.errors, [])

    def _delete_edit_images_refresh(self):
        original = self.current()
        self.page.locator('#creative-assistant > summary').click()
        self.page.locator('#formal-script-instruction').fill('删除第1镜')
        self.page.locator('#formal-script-instruction-submit').click()
        with self.page.expect_response(lambda r: r.request.method == 'POST' and '/confirm' in r.url) as response:
            self.page.locator('[data-confirm-script-revision]').click()
        self.assertTrue(response.value.ok, response.value.text())
        expect(self.page.locator('#creative-shot-count')).to_have_text(f"{len(original['shots']) - 1} 镜")
        survivor = original['shots'][1]['shot_id']
        target = self.page.locator(f'[data-script-shot-key="{survivor}"][data-script-field="subtitle"]')
        expect(target).to_be_visible()
        with self.page.expect_response(lambda r: r.request.method == 'POST' and '/script/table?' in r.url) as written:
            target.fill('删除后幸存镜字幕')
            target.blur()
        self.assertTrue(written.value.ok, written.value.text())
        expect(self.page.locator('#formal-script-save-state')).to_contain_text('已保存')
        self.assertEqual(self.current()['shots'][0]['shot_id'], survivor)
        self.assertEqual(self.current()['shots'][0]['subtitle'], '删除后幸存镜字幕')
        self.page.locator('[data-creative-panel="images"]').click()
        expect(self.page.locator('[data-image-shot]')).to_have_count(len(original['shots']) - 1)
        self.page.reload()
        expect(self.page.locator('[data-image-shot]')).to_have_count(len(original['shots']) - 1)
        self.page.locator('[data-creative-panel="script"]').click()
        self.page.locator('[data-creative-edit-mode="table"]').click()
        expect(target).to_have_value('删除后幸存镜字幕')
        self.assertEqual(self.errors, [])
        self.page.screenshot(path=str(self.root / 'deleted-survivor.png'), full_page=True)

    def test_old_draft_after_new_brief_can_be_read_and_discarded_in_ui(self):
        old = self.current()
        self.base.generated._confirmed_brief(expected_project_version=old['project_version'],
            required_content='匿名更新后的活动流程')
        self.page.reload()
        self.page.locator('[data-creative-edit-mode="table"]').click()
        expect(self.cell('subtitle')).to_have_value('原有项目的待保存字幕')
        self.page.locator('[data-creative-panel="images"]').click()
        expect(self.page.locator('[data-image-shot]')).to_have_count(len(old['shots']))
        self.page.once('dialog', lambda dialog: dialog.accept())
        with self.page.expect_response(lambda r: r.request.method == 'POST' and '/progress/discard?' in r.url) as response:
            self.page.locator('#formal-script-discard').click()
        self.assertTrue(response.value.ok, response.value.text())
        expect(self.page.locator('#formal-script-discard')).to_be_disabled()
        self.page.reload()
        self.assertFalse(self.current()['can_save_version'])
        self.assertEqual(self.current()['base_version_id'], old['base_version_id'])
        self.assertEqual(self.errors, [])

    def test_real_image_reuse_reason_updates_other_shots_and_survives_reload(self):
        from tests import test_mvp_v2_formal_image_workspace as image_fixture
        support = image_fixture.FormalImageWorkspaceTests('runTest')
        for name, value in dict(base=self.base, store=self.store, projects=self.base.projects,
            delivery=self.base.delivery, resolver=self.base.resolver, token=self.token,
            project_id=self.project_id, workspace_id=self.workspace_id, pool=self.images.services.images).items():
            setattr(support, name, value)
        support._install_pass_images(3)
        current = self.current()
        images = self.images.read_current(self.token, **self.scope)
        key = images['shots'][0]['candidates'][0]['image_key']
        # Seed frozen anonymous multi-source relations, then exercise the real
        # UI/API mutation; the test does not claim to validate package adoption.
        from mvp_v2.workspace_store import new_id
        with self.store.transaction() as connection:
            image = connection.execute(
                'SELECT * FROM project_images WHERE project_image_id=?', (key,)).fetchone()
            self.assertIsNotNone(image)
            for row in (11, 42):
                connection.execute(
                    'INSERT INTO project_image_package_source_links VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (new_id('package_source'), key, 'anonymous-source-fixture', 1,
                     'anonymous-media', image['image_sha256'], '匿名多来源.xlsx', '创意库', row,
                     image['created_by_actor_id'], image['created_at']))
        def source_evidence():
            with self.store.connection() as connection:
                return {table: sorted(tuple(row) for row in connection.execute('SELECT * FROM ' + table))
                    for table in ('document_images', 'project_images', 'project_image_source_links',
                                  'project_image_analysis_versions', 'project_image_analysis_current_links',
                                  'project_image_package_source_links', 'project_versions')}
        with self.store.connection() as connection:
            self.assertGreaterEqual(connection.execute(
                'SELECT COUNT(*) FROM project_image_package_source_links WHERE project_image_id=?',
                (key,)).fetchone()[0], 2)
        evidence_before = source_evidence()
        ids = [shot['shot_id'] for shot in current['shots'][:3]]
        self.images.save_progress(self.token, **self.scope, base_version_id=current['base_version_id'],
            expected_project_version=current['project_version'], expected_revision=0,
            expected_script_revision=current['revision'], idempotency_key='seed-three-same-image',
            selections=[dict(shot_id=identity, image_key=key, checked=False, decision_kind='image_confirmed',
                reuse_reason='', temporary_reason='', query='') for identity in ids])
        self.page.goto(self.url + '?panel=images')
        field = self.page.locator('[data-image-shot-reuse-reason="0"]')
        field.fill('同一场景分别说明不同镜头关系')
        field.blur()
        self.page.once('dialog', lambda dialog: dialog.accept())
        with self.page.expect_response(lambda r: r.request.method == 'PATCH' and '/image-workspace/progress?' in r.url) as response:
            self.page.locator('[data-image-reuse-apply="0"]').click()
        self.assertTrue(response.value.ok, response.value.text())
        self.page.reload()
        self.page.locator('#creative-image-scope').select_option('all')
        for index in (0, 1, 2):
            expect(self.page.locator(f'[data-image-shot-reuse-reason="{index}"]')).to_have_value('同一场景分别说明不同镜头关系')
        progress = self.images.read_progress(self.token, **self.scope)
        selected = [s for s in progress['selections'] if s['shot_id'] in ids]
        self.assertEqual(len(selected), 3)
        self.assertTrue(all(s['reuse_reason'] == '同一场景分别说明不同镜头关系' and not s['checked'] for s in selected))
        self.assertEqual(source_evidence(), evidence_before)
        self.assertEqual(self.errors, [])

    def test_eighth_shot_long_text_picker_cancel_preserves_position_and_focus(self):
        current = self.current()
        self.assertGreaterEqual(len(current['shots']), 9)
        target = current['shots'][7]
        other = current['shots'][8]
        long_text = '匿名现场主体按确认流程完成演示，画面保留环境与动作关系。' * 35
        self.script.apply_table_change(self.token, **self.scope,
            expected_project_version=current['project_version'], expected_revision=current['revision'],
            shot_no=target['shot_no'], shot_id=target['shot_id'], field='visual', value=long_text,
            idempotency_key='long-eighth-shot-layout')
        before = self.current()
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                'SELECT COUNT(*) FROM project_images WHERE project_id=?',
                (self.project_id,)).fetchone()[0], 0)
        image_writes = []
        self.page.on('request', lambda request: image_writes.append(request.url)
            if request.method in {'POST', 'PATCH', 'DELETE'} and
            ('/images/' in request.url or '/image-workspace' in request.url) else None)
        self.page.set_viewport_size({'width':1440, 'height':900})
        self.page.reload()
        self.page.locator('[data-creative-edit-mode="shot"]').click()
        first_visual = self.page.locator('[data-script-shot="1"][data-script-field="visual"]')
        expect(first_visual).to_be_visible()
        self.assertLess(first_visual.bounding_box()['y'], 700)
        import os
        from pathlib import Path
        evidence = os.environ.get('MPLUS_RV07_LAYOUT_EVIDENCE_ROOT')
        if evidence:
            Path(evidence).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(evidence)/'editor-first-screen.png'))
        self.page.locator('[data-creative-batch-toggle]').click()
        self.page.locator(f'[data-creative-select-shot="{other["shot_id"]}"]').check()
        self.page.locator(f'[data-creative-shot="{target["shot_id"]}"]').click()
        field = self.page.locator(f'[data-script-shot-key="{target["shot_id"]}"][data-script-field="visual"]')
        expect(field).to_have_value(long_text)
        trigger = self.page.get_by_role('button', name='换图', exact=True)
        for width in (1440, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 900})
                trigger.scroll_into_view_if_needed()
                trigger.click()
                picker = self.page.get_by_role('dialog', name='第 8 镜', exact=True)
                expect(picker).to_be_visible()
                expect(picker.locator('[data-picker-use]')).to_be_disabled()
                expect(picker.locator('.creative-picker-selected')).to_have_count(0)
                query = picker.locator('[data-picker-query]')
                query.fill('尚未提交的临时选图查询')
                query.press('Escape')
                expect(picker).not_to_be_visible()
                expect(trigger).to_be_focused()
                expect(self.page.locator(f'[data-creative-shot="{target["shot_id"]}"]')).to_have_attribute('aria-current','true')
                expect(self.page.locator(f'[data-creative-select-shot="{other["shot_id"]}"]')).to_be_checked()
                expect(field).to_have_value(long_text)
                dimensions = self.page.locator('html').evaluate('node => [node.scrollWidth, node.clientWidth]')
                self.assertLessEqual(dimensions[0], dimensions[1] + 1)
                field.scroll_into_view_if_needed()
                field.focus()
                expect(field).to_be_focused()
                if evidence:
                    self.page.screenshot(path=str(Path(evidence)/f'eighth-shot-{width}.png'))
        self.assertEqual(self.current(), before)
        self.assertEqual(image_writes, [])
        self.assertEqual(self.errors, [])
