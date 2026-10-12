"""Project-scoped editor/reviewer sessions through actual HTTP and persistent services."""
import json
import os
from pathlib import Path
import unittest
from playwright.sync_api import expect
from mvp_v2 import local_ui
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.task_service import TaskService
from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_review_workspace as review_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture
from tests.test_mvp_v2_formal_browser_journey import _approve_remaining_review, _final_review_note
from tests.test_mvp_v2_library_browse import library_confirmation_agent


class ProjectRolesBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_project_editor_saves_then_reviewer_confirms_and_downloads(self):
        f = review_fixture.FormalReviewWorkspaceTests('runTest'); f.setUp(); self.addCleanup(f.doCleanups)
        identity = LocalIdentityAdapter(f.store); scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        actors = {}; tokens = {}
        for role in ('editor', 'reviewer'):
            workspace = f.base.projects.get_workspace(f.token, f.workspace_id)
            actors[role] = identity.create_actor(f.token, **scope, display_name='匿名项目'+role,
                auth_source='local', role=role, expected_workspace_version=workspace['workspace_version'])
            tokens[role] = identity.issue_auth_session(actor_id=actors[role]['actor_id'],
                token='anonymous-project-role-'+role)['auth_token']
        with f.store.connection() as c:
            for role in actors:
                assignments = c.execute('SELECT role_id, project_id FROM role_assignments WHERE actor_id=?',
                    (actors[role]['actor_id'],)).fetchall()
                self.assertEqual([tuple(r) for r in assignments], [(role, f.project_id)])
        script = f.base.base.workspace
        before = script.read_current(tokens['editor'], **scope)
        agent = library_confirmation_agent(root=f.root, store=f.store, projects=f.base.projects, service=None)
        agent.script_revision = FormalRevisionService(projects=f.base.projects, tasks=TaskService(f.store),
            workspace=script, image_workspace=f.base.workspace, writes_enabled=True)
        app = local_ui.create_app(agent_api=agent, formal_script_service=script,
            formal_image_service=f.base.workspace, formal_review_service=f.review,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.root/'runtime'),data_root=f.root/'runtime'),
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, tokens['editor'], f.workspace_id)
        errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
        path = origin+'/projects/'+f.project_id
        page.goto(path+'/script'); page.locator('[data-creative-edit-mode="table"]').click()
        with page.expect_response(lambda r: '/script/table?' in r.url and r.request.method=='POST') as edited:
            field=page.locator('[data-script-shot="1"][data-script-field="subtitle"]')
            field.fill('普通编辑通过真实界面修改字幕'); field.blur()
        self.assertEqual(edited.value.status,200,edited.value.text())
        page.locator('#creative-save > summary').click()
        page.locator('#formal-script-save-reason').fill('普通编辑保存字幕版本')
        with page.expect_response(lambda r:'/script/save?' in r.url and r.request.method=='POST',timeout=60000) as proposal:
            page.locator('#formal-script-save').click()
        self.assertEqual(proposal.value.status,200,proposal.value.text()); task=proposal.value.json()['task_id']
        with page.expect_response(lambda r:f'/tasks/{task}/confirm' in r.url,timeout=60000) as saved:
            page.locator('[data-confirm-script-revision]').click()
        self.assertEqual(saved.value.status,200,saved.value.text())
        page.reload();page.locator('[data-creative-edit-mode="table"]').click()
        expect(page.locator('[data-script-shot="1"][data-script-field="subtitle"]')).to_have_value('普通编辑通过真实界面修改字幕')
        after=script.read_current(tokens['editor'],**scope)
        self.assertNotEqual(after['base_version_id'],before['base_version_id'])
        self.assertEqual(after['shots'][1:],before['shots'][1:])
        # Independent browser context: neither role ever receives the owner token.
        reviewer=page.context.browser.new_context();self.addCleanup(reviewer.close)
        reviewer.add_init_script('localStorage.setItem("mplus.agent.auth",'+json.dumps(tokens['reviewer'])+');localStorage.setItem("mplus.agent.workspace",'+json.dumps(f.workspace_id)+');')
        manager=reviewer.new_page();manager.set_default_timeout(15000)
        manager.on('pageerror',lambda e:errors.append(str(e)))
        manager.goto(path+'/review')
        manager.wait_for_load_state('networkidle')
        with manager.expect_response(lambda r:'/review/start?' in r.url and r.request.method=='POST') as started:
            manager.locator('#formal-review-start').click()
        self.assertEqual(started.value.status,200,started.value.text())
        page.goto(path+'/review')
        page.wait_for_load_state('networkidle')
        expect(page.locator('[data-review-decision="approve"]')).to_have_count(0)
        review_model = started.value.json()
        denied = page.request.post(origin+'/api/formal/projects/'+f.project_id+
            '/review/decisions?workspace_id='+f.workspace_id,
            headers={'X-MPlus-Agent-Auth':tokens['editor'], 'Origin':origin},
            data=dict(expected_project_version=review_model['project_version'],
                issue_keys=[review_model['issues'][0]['issue_key']], decision='approve',
                note='普通编辑不得越权批准', idempotency_key='editor-forbidden-approval'))
        self.assertEqual(denied.status,403,denied.text())
        with f.store.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM version_review_human_decisions').fetchone()[0],0)
        _approve_remaining_review(manager,f.store.database_path)
        expect(manager.locator('#formal-review-state')).to_contain_text('待整片确认')
        _final_review_note(manager).fill('匿名角色验收：已逐项核对当前版本')
        manager.locator('#formal-review-final-submit').click()
        expect(manager.locator('#formal-review-state')).to_contain_text('整片已确认',timeout=60000)
        manager.reload();expect(manager.locator('#formal-review-state')).to_contain_text('整片已确认')
        with manager.expect_download(timeout=60000) as download: manager.locator('#formal-delivery-excel').click()
        self.assertIsNone(download.value.failure())
        with f.store.connection() as c:
            reviewers={r[0] for r in c.execute('SELECT reviewer_actor_id FROM version_review_human_decisions')}
            self.assertEqual(reviewers,{actors['reviewer']['actor_id']})
        self.assertEqual(errors,[])
        # Explicitly reconfirming an image still requires review.record.
        current = script.read_current(tokens['editor'], **scope)
        images = f.base.workspace.read_current(tokens['editor'], **scope)
        progress = f.base.workspace.read_progress(tokens['editor'], **scope)
        first = images['shots'][0]
        f.base.workspace.save_progress(tokens['editor'], **scope,
            base_version_id=current['base_version_id'], expected_project_version=current['project_version'],
            expected_revision=progress['revision'], expected_script_revision=current['revision'],
            selections=[dict(shot_id=first['shot_id'], image_key=first['selected_image_key'],
                checked=True, decision_kind='image_confirmed', temporary_reason='')],
            idempotency_key='editor-explicit-image-reconfirmation')
        with f.store.connection() as c:
            database_before_rejection = '\n'.join(c.iterdump())
        with self.assertRaises(WorkspaceServiceError) as rejected:
            f.base.workspace.prepare_working_version(tokens['editor'], **scope,
                expected_project_version=current['project_version'], expected_script_revision=current['revision'])
        self.assertEqual(rejected.exception.code, 'PERMISSION_DENIED')
        with f.store.connection() as c:
            self.assertEqual('\n'.join(c.iterdump()), database_before_rejection)
        evidence=os.environ.get('MPLUS_ROLE_EVIDENCE_ROOT')
        if evidence:
            root=Path(evidence);root.mkdir(parents=True,exist_ok=True)
            manager.screenshot(path=str(root/'reviewer-confirmed.png'),full_page=True)
            (root/'result.json').write_text(json.dumps(dict(roles=['editor','reviewer'],project_scoped=True,
                owner_used_only_for_fixture_and_identity_setup=True,editor_approval_status=403,
                persisted_child_version=True,non_target_shots_unchanged=True,reviewer_decisions_verified=True,
                final_confirmation_reload=True,excel_download=True,external_provider=False),indent=2)+'\n')
