"""Actual version publication and CURRENT projection across a project switch."""
import threading
import unittest

from playwright.sync_api import expect
from starlette.concurrency import run_in_threadpool
from mvp_v2 import local_ui
from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_workbench import VersionWorkbenchService, VersionWorkbenchServices
from mvp_v2.ui_workbench import UiWorkbenchService, UiWorkbenchServices
from mvp_v2.manual_version_commands import ManualVersionCommandService, ManualVersionCommandServices
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from tests import test_mvp_v2_formal_review_workspace as fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class PublicationLateBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_published_current_late_response_keeps_other_project_and_single_publication(self):
        f = fixture.FormalReviewWorkspaceTests('runTest')
        f.setUp(); self.addCleanup(f.doCleanups)
        confirmed = f._confirm_current()
        projects, versions, tasks = fixture.ProjectService(f.store), fixture.VersionService(f.store), fixture.TaskService(f.store)
        original = projects.get_project(f.token, f.project_id)
        version_id = original['current_version_id']
        versions.select_candidate(f.token, version_id=version_id,
            expected_project_version=confirmed['project_version'], reason='匿名发布投影验收')
        current = projects.get_project(f.token, f.project_id)
        f.review.prepare_excel_delivery(f.token, workspace_id=f.workspace_id, project_id=f.project_id,
            expected_project_version=current['project_version'])
        ws = projects.get_workspace(f.token, f.workspace_id)
        other = projects.create_project(f.token, workspace_id=f.workspace_id, name='匿名投影项目乙',
            category_id='launch', expected_workspace_version=ws['workspace_version'])
        other_before = projects.get_project(f.token, other['project_id'])
        artifacts = f.review.services.artifacts
        projector = fixture.VersionPublicationProjectionService(f.store, artifacts)
        commands = ManualVersionCommandService(ManualVersionCommandServices(
            store=f.store, projects=projects, versions=versions, artifacts=artifacts,
            publication_projection=projector))
        workbench = UiWorkbenchService(UiWorkbenchServices(
            versions=VersionWorkbenchService(VersionWorkbenchServices(store=f.store, projects=projects,
                versions=versions, tasks=tasks, artifacts=artifacts,
                confirmations=ActionConfirmationService(f.store, b'p'*32),
                idempotency=ToolIdempotencyService(f.store))),
            assets=None, manual_versions=commands))
        agent = fixture.ReadonlyAgentService(fixture.ReadonlyAgentServices(store=f.store,
            identity=fixture.LocalIdentityAdapter(f.store), projects=projects, tasks=tasks,
            versions=versions, artifacts=artifacts))
        app = local_ui.create_app(agent_api=agent, ui_workbench=workbench,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.root/'runtime'), data_root=f.root/'runtime'),
            formal_brief_upload_factory=uploaded_document)
        committed, release = threading.Event(), threading.Event()
        requests = []
        @app.middleware('http')
        async def delay_publication(request, call_next):
            response = await call_next(request)
            if request.url.path == '/api/manual/versions/actions/execute':
                requests.append(response.status_code)
                committed.set()
                await run_in_threadpool(release.wait, 25)
            return response
        page, origin = self.open_page(app, f.token, f.workspace_id)
        self.addCleanup(release.set)
        page.goto(origin + '/projects/' + f.project_id + '/script?view=versions')
        try:
            page.locator('[data-version-action="publish_version"]').click()
        except Exception:
            print(page.locator('body').inner_text())
            raise
        page.locator('#version-confirm-check').check()
        page.locator('#version-confirm-execute').click()
        self.assertTrue(committed.wait(20))
        self.assertEqual(versions.list_publications(f.token, version_id)[-1]['projection_status'], 'published')
        self.assertTrue(projector.current_path.is_symlink())
        target = projector.current_path.resolve()
        with f.store.connection() as connection:
            run_id = connection.execute('SELECT run_id FROM project_versions WHERE version_id=?', (version_id,)).fetchone()[0]
        self.assertEqual(target.name, run_id)
        page.locator('#version-project-select').select_option(other['project_id'])
        expect(page.locator('#version-tree .version-node')).to_have_count(0)
        with page.expect_response(lambda r: r.url.endswith('/api/manual/versions/actions/execute')):
            release.set()
        page.wait_for_timeout(200)
        expect(page.locator('#version-project-select')).to_have_value(other['project_id'])
        expect(page.locator('#version-confirmation')).to_be_hidden()
        expect(page.locator('#version-tree .version-node')).to_have_count(0)
        page.locator('#version-project-select').select_option(f.project_id)
        expect(page.locator('#version-tree .is-published')).to_have_count(1)
        expect(page.locator('#version-confirmation')).to_be_hidden()
        page.reload()
        expect(page.locator('#version-tree .is-published')).to_have_count(1)
        expect(page.locator('#version-confirmation')).to_be_hidden()
        self.assertEqual(requests, [200])
        self.assertEqual(projector.current_path.resolve(), target)
        self.assertEqual(projects.get_project(f.token, other['project_id']), other_before)
        with f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM version_publications').fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM manual_command_results r JOIN manual_command_confirmations c USING(command_id) WHERE c.tool_name='publish_version'").fetchone()[0], 1)
            event = connection.execute("SELECT status, attempts FROM outbox WHERE event_type='project.version_published'").fetchone()
            self.assertEqual((event['status'], event['attempts']), ('published', 1))
