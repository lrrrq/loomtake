"""Anonymous real HTTP manual command: committed response loss and reload recovery."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
import uvicorn
from mvp_v2 import local_ui
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from tests.test_mvp_v2_ui_workbench import UiWorkbenchFixture


class ManualReceiptBrowserTests(unittest.TestCase):
    def test_knowledge_late_approval_keeps_other_project_and_return_state(self):
        from tests.test_mvp_v2_ui_governance import GovernanceUiFixture
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        f = GovernanceUiFixture(Path(root.name)); self.addCleanup(f.client.close)
        prepared = f.post('/api/agent/governance/knowledge/candidates/prepare', {
            'agent_session_id': f.session, 'scope': 'project',
            'statement': '匿名恢复验证规则：先展示已确认事实',
            'evidence_refs': [f.projects.get_current_brief_snapshot(f.auth, f.project['project_id'])['brief_ref']],
            'idempotency_key': 'anonymous-knowledge-recovery-candidate'})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        created = f.post('/api/agent/governance/knowledge/candidates/execute', {
            'agent_session_id': f.session, 'parameters': prepared.json()['parameters'],
            'confirmation_token': prepared.json()['confirmation_token']})
        self.assertEqual(created.status_code, 200, created.text)
        candidate_id = created.json()['result']['candidate_id']
        ws = f.projects.get_workspace(f.auth, f.workspace_id)
        other = f.projects.create_project(f.auth, workspace_id=f.workspace_id,
            name='匿名知识项目乙', category_id='launch',
            expected_workspace_version=ws['workspace_version'])
        other_before = f.projects.get_project(f.auth, other['project_id'])
        app = local_ui.create_app(agent_api=f.app.state.agent_api, ui_governance=f.app.state.ui_governance,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.data_root), data_root=f.data_root),
            formal_brief_upload_factory=uploaded_document)
        from starlette.concurrency import run_in_threadpool
        committed, release = threading.Event(), threading.Event()
        requests = []
        @app.middleware('http')
        async def hold_approval(request, call_next):
            response = await call_next(request)
            if request.url.path == '/api/manual/knowledge/approvals/execute':
                requests.append(response.status_code)
                committed.set()
                await run_in_threadpool(release.wait, 20)
            return response
        page, origin = self.open_page(app, f.auth, f.workspace_id)
        self.addCleanup(release.set)
        page.goto(origin + '/knowledge?project_id=' + f.project['project_id'])
        page.locator(f'[data-approval-evidence="{candidate_id}"]').fill('匿名迟到响应测试依据')
        page.locator(f'[data-knowledge-approve="{candidate_id}"]').click()
        page.locator('#governance-confirm-check').check()
        page.locator('#governance-confirm-execute').click()
        self.assertTrue(committed.wait(10))
        page.locator('#governance-project-select').select_option(other['project_id'])
        expect(page.locator('#governance-project-select')).to_have_value(other['project_id'])
        expect(page.locator('#governance-confirmation')).to_be_hidden()
        expect(page.locator('#pending-knowledge-count')).to_have_text('0 条建议')
        with page.expect_response(lambda r: r.url.endswith('/api/manual/knowledge/approvals/execute')):
            release.set()
        page.wait_for_timeout(200)
        expect(page.locator('#governance-project-select')).to_have_value(other['project_id'])
        expect(page.locator('#governance-confirmation')).to_be_hidden()
        expect(page.locator('#knowledge-rules')).not_to_contain_text('先展示已确认事实')
        page.locator('#governance-project-select').select_option(f.project['project_id'])
        expect(page.locator('#knowledge-rules')).to_contain_text('先展示已确认事实')
        expect(page.locator('#pending-knowledge-count')).to_have_text('0 条建议')
        expect(page.locator('#governance-confirmation')).to_be_hidden()
        page.reload()
        expect(page.locator('#knowledge-rules')).to_contain_text('先展示已确认事实')
        expect(page.locator('#governance-confirmation')).to_be_hidden()
        self.assertEqual(requests, [200])
        self.assertEqual(f.projects.get_project(f.auth, other['project_id']), other_before)
        with f.store.connection() as connection:
            for table in ('knowledge_approvals', 'knowledge_rules', 'manual_command_results'):
                self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 1)

    def test_knowledge_approval_response_loss_reload_recovers_one_approval(self):
        from tests.test_mvp_v2_ui_governance import GovernanceUiFixture
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        f = GovernanceUiFixture(Path(root.name)); self.addCleanup(f.client.close)
        prepared = f.post('/api/agent/governance/knowledge/candidates/prepare', {
            'agent_session_id': f.session, 'scope': 'project',
            'statement': '匿名恢复验证规则：先展示已确认事实',
            'evidence_refs': [f.projects.get_current_brief_snapshot(f.auth, f.project['project_id'])['brief_ref']],
            'idempotency_key': 'anonymous-knowledge-recovery-candidate'})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        created = f.post('/api/agent/governance/knowledge/candidates/execute', {
            'agent_session_id': f.session, 'parameters': prepared.json()['parameters'],
            'confirmation_token': prepared.json()['confirmation_token']})
        self.assertEqual(created.status_code, 200, created.text)
        candidate_id = created.json()['result']['candidate_id']
        app = local_ui.create_app(agent_api=f.app.state.agent_api, ui_governance=f.app.state.ui_governance,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.data_root), data_root=f.data_root),
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.auth, f.workspace_id)
        requests = []
        def lose_response(route):
            requests.append(route.request.post_data_json)
            response = route.fetch()
            self.assertEqual(response.status, 200, response.text())
            if len(requests) == 1:
                route.abort('connectionreset')
            else:
                route.fulfill(response=response)
        page.route('**/api/manual/knowledge/approvals/execute', lose_response)
        page.goto(origin + '/knowledge?project_id=' + f.project['project_id'])
        page.locator(f'[data-approval-evidence="{candidate_id}"]').fill('已核对匿名候选与冻结来源，仅用于隔离恢复验收')
        page.locator(f'[data-knowledge-approve="{candidate_id}"]').click()
        page.locator('#governance-confirm-check').check()
        page.locator('#governance-confirm-execute').click()
        expect(page.locator('#governance-confirm-expiry')).to_contain_text('原请求已保留')
        with f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM knowledge_approvals').fetchone()[0], 1)
        page.reload()
        expect(page.locator('#governance-confirm-risk')).to_have_text('上次操作结果待核对')
        page.locator('#governance-confirm-check').check()
        page.locator('#governance-confirm-execute').click()
        expect(page.locator('#governance-confirmation')).to_be_hidden()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        with f.store.connection() as connection:
            for table in ('knowledge_approvals', 'knowledge_rules', 'manual_command_results'):
                self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 1)
        page.reload()
        expect(page.locator('#pending-knowledge-count')).to_have_text('0 条建议')
        expect(page.locator('#governance-confirmation')).to_be_hidden()

    def test_candidate_late_success_keeps_other_project_and_recovers_on_return(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        f = UiWorkbenchFixture(Path(root.name)); self.addCleanup(f.client.close)
        version = f.add_version('匿名待选版本', human_review_status='quality_pass')
        f.versions.record_review(f.auth_token, version_id=version['version_id'], decision='quality_pass',
            evidence_ref=f"artifact://run/{version['run_id']}/run_report",
            expected_project_version=f.projects.get_project(f.auth_token, f.project['project_id'])['project_version'])
        ws = f.projects.get_workspace(f.auth_token, f.workspace_id)
        b = f.projects.create_project(f.auth_token, workspace_id=f.workspace_id,
            name='匿名迟到回执项目乙', category_id='launch',
            expected_workspace_version=ws['workspace_version'])
        b_before = f.projects.get_project(f.auth_token, b['project_id'])
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=f.store, identity=f.identity,
            projects=f.projects, tasks=f.tasks, versions=f.versions, artifacts=RunArtifactResolver(f.runs_root)))
        app = local_ui.create_app(agent_api=agent, ui_workbench=f.workbench,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store, artifact_root=f.data_root), data_root=f.data_root),
            formal_brief_upload_factory=uploaded_document)
        from starlette.concurrency import run_in_threadpool
        committed = threading.Event()
        release = threading.Event()
        requests = []
        @app.middleware('http')
        async def hold_committed_response(request, call_next):
            response = await call_next(request)
            if request.url.path == '/api/manual/versions/actions/execute':
                requests.append(response.status_code)
                committed.set()
                await run_in_threadpool(release.wait, 20)
            return response
        page, origin = self.open_page(app, f.auth_token, f.workspace_id)
        self.addCleanup(release.set)
        page.goto(origin + f'/projects/{f.project["project_id"]}/script?view=versions')
        page.locator('[data-version-action="select_candidate_version"]').click()
        page.locator('#version-confirm-check').check()
        page.locator('#version-confirm-execute').click()
        self.assertTrue(committed.wait(10), 'Actual version command must commit before switching')
        with f.store.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM candidate_selections').fetchone()[0], 1)
        page.locator('#version-project-select').select_option(b['project_id'])
        expect(page.locator('#version-project-select')).to_have_value(b['project_id'])
        expect(page.locator('#version-confirmation')).to_be_hidden()
        expect(page.locator('#version-tree .version-node')).to_have_count(0)
        with page.expect_response(lambda r: r.url.endswith('/api/manual/versions/actions/execute')):
            release.set()
        # Wait for the promise continuation, then assert the current project is unchanged.
        page.wait_for_timeout(200)
        expect(page.locator('#version-project-select')).to_have_value(b['project_id'])
        expect(page.locator('#version-confirmation')).to_be_hidden()
        expect(page.locator('#version-tree .version-node')).to_have_count(0)
        page.locator('#version-project-select').select_option(f.project['project_id'])
        expect(page.locator(f'[data-version-read="{version["version_id"]}"]')).to_be_visible()
        expect(page.locator('#version-tree')).to_contain_text('当前候选')
        expect(page.locator('#version-confirmation')).to_be_hidden()
        page.reload()
        expect(page.locator('#version-tree')).to_contain_text('当前候选')
        expect(page.locator('#version-confirmation')).to_be_hidden()
        self.assertEqual(requests, [200])
        self.assertEqual(f.projects.get_project(f.auth_token, b['project_id']), b_before)
        with f.store.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM candidate_selections').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM manual_command_results').fetchone()[0], 1)

    def test_candidate_response_loss_reload_reuses_original_command(self):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        f = UiWorkbenchFixture(Path(root.name)); self.addCleanup(f.client.close)
        version = f.add_version('匿名待选版本', human_review_status='quality_pass')
        f.versions.record_review(f.auth_token, version_id=version['version_id'], decision='quality_pass',
            evidence_ref=f"artifact://run/{version['run_id']}/run_report",
            expected_project_version=f.projects.get_project(f.auth_token, f.project['project_id'])['project_version'])
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=f.store, identity=f.identity,
            projects=f.projects, tasks=f.tasks, versions=f.versions, artifacts=RunArtifactResolver(f.runs_root)))
        app = local_ui.create_app(agent_api=agent, ui_workbench=f.workbench,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store, artifact_root=f.data_root), data_root=f.data_root),
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.auth_token, f.workspace_id)
        requests=[]
        def lose_response(route):
            requests.append(route.request.post_data_json)
            response = route.fetch()
            self.assertEqual(response.status, 200)
            if len(requests) == 1: route.abort('connectionreset')
            else: route.fulfill(response=response)
        page.route('**/api/manual/versions/actions/execute', lose_response)
        page.goto(origin + f'/projects/{f.project["project_id"]}/script?view=versions')
        try:
            page.locator('[data-version-action="select_candidate_version"]').click()
        except Exception:
            print(page.locator('body').inner_text())
            raise
        page.locator('#version-confirm-check').check()
        page.locator('#version-confirm-execute').click()
        expect(page.locator('#version-confirm-expiry')).to_contain_text('原请求已保留')
        with f.store.connection() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM candidate_selections').fetchone()[0], 1)
        page.reload()
        expect(page.locator('#version-confirm-risk')).to_have_text('上次操作结果待核对')
        page.locator('#version-confirm-check').check()
        page.locator('#version-confirm-execute').click()
        expect(page.locator('#version-confirmation')).to_be_hidden()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        with f.store.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM candidate_selections').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM manual_command_results').fetchone()[0], 1)
        page.reload()
        expect(page.locator('#version-confirmation')).to_be_hidden()

    def open_page(self, app, auth_token, workspace_id):
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        allowed = patch.object(local_ui, 'ALLOWED_ORIGINS', {*local_ui.ALLOWED_ORIGINS, origin}); allowed.start(); self.addCleanup(allowed.stop)
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True); thread.start()
        def stop():
            server.should_exit = True; thread.join(10); sock.close()
        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline: time.sleep(.02)
        self.assertTrue(server.started)
        pw = sync_playwright().start(); self.addCleanup(pw.stop)
        browser = pw.chromium.launch(headless=True); self.addCleanup(browser.close)
        page = browser.new_page(); page.set_default_timeout(15000)
        page.add_init_script('localStorage.setItem("mplus.agent.auth", '+json.dumps(auth_token)+');localStorage.setItem("mplus.agent.workspace", '+json.dumps(workspace_id)+');')
        return page, origin

    def test_strategy_browsing_is_passive_explicit_analysis_survives_refresh(self):
        from tests.test_mvp_v2_ui_governance import GovernanceUiFixture
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        f = GovernanceUiFixture(Path(root.name)); self.addCleanup(f.client.close)
        app = local_ui.create_app(agent_api=f.app.state.agent_api, ui_governance=f.app.state.ui_governance,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store, artifact_root=f.data_root), data_root=f.data_root),
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.auth, f.workspace_id)
        def count():
            with f.store.connection() as connection:
                return connection.execute("SELECT COUNT(*) FROM tasks WHERE task_type='get_creative_strategies'").fetchone()[0]
        page.goto(origin + '/knowledge?project_id=' + f.project['project_id'])
        expect(page.locator('[data-strategy-analyze]')).to_be_visible()
        self.assertEqual(count(), 0)
        page.reload()
        expect(page.locator('[data-strategy-analyze]')).to_be_visible()
        self.assertEqual(count(), 0)
        page.locator('[data-strategy-analyze]').click()
        expect(page.locator('.strategy-card')).to_have_count(3)
        self.assertEqual(count(), 1)
        page.reload()
        expect(page.locator('.strategy-card')).to_have_count(3)
        self.assertEqual(count(), 1)
        def business_snapshot():
            with f.store.connection() as connection:
                return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM {table}')]
                    for table in ('projects', 'project_brief_snapshots', 'project_versions', 'candidate_selections')}
        before_browsing = business_snapshot()
        brief_before = f.projects.get_current_brief_snapshot(f.auth, f.project['project_id'])
        brief_payload = f.briefs.read(brief_before['brief_ref'], brief_before['brief_sha256'])
        mutations = []
        page.on('request', lambda request: mutations.append(request.url)
            if request.method == 'POST' and '/governance/strategies/' in request.url else None)
        for index in (0, 2, 1):
            card = page.locator('.strategy-card').nth(index)
            card.scroll_into_view_if_needed()
            card.locator('header').click()
            expect(card.locator('h3')).to_be_visible()
        page.goto(origin + '/projects')
        page.goto(origin + '/knowledge?project_id=' + f.project['project_id'])
        expect(page.locator('.strategy-card')).to_have_count(3)
        page.reload()
        expect(page.locator('.strategy-card')).to_have_count(3)
        self.assertEqual(mutations, [])
        self.assertEqual(count(), 1)
        self.assertEqual(business_snapshot(), before_browsing)
        self.assertEqual(f.projects.get_current_brief_snapshot(f.auth, f.project['project_id']), brief_before)
        self.assertEqual(f.briefs.read(brief_before['brief_ref'], brief_before['brief_sha256']), brief_payload)

    def test_old_confirmed_images_are_read_then_explicitly_connected_and_reloaded(self):
        from tests import test_mvp_v2_project_image_authority as image_fixture
        from mvp_v2.formal_generation_service import FormalGenerationService, FormalGenerationServices
        from mvp_v2.task_service import TaskService
        from mvp_v2.version_service import VersionService
        from mvp_v2.knowledge_service import KnowledgeService
        from mvp_v2.identity_service import LocalIdentityAdapter
        f = image_fixture.ProjectImageAuthorityTests('runTest'); f.setUp(); self.addCleanup(f.tearDown)
        f._confirmed_first()
        tasks = TaskService(f.store)
        generation = FormalGenerationService(FormalGenerationServices(briefs=f.briefs, projects=f.projects,
            tasks=tasks, artifacts=RunArtifactResolver(f.root/'runs'), images=f.pool,
            knowledge=KnowledgeService(f.store)), runs_root=f.root/'runs', source_root=f.root/'source')
        agent=ReadonlyAgentService(ReadonlyAgentServices(store=f.store, identity=LocalIdentityAdapter(f.store),
            projects=f.projects, tasks=tasks, versions=VersionService(f.store), artifacts=RunArtifactResolver(f.root/'runs')))
        from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
        from mvp_v2.version_delivery import VersionDeliveryService
        scripts=FormalScriptWorkspaceService(FormalScriptWorkspaceServices(store=f.store, projects=f.projects,
            delivery=VersionDeliveryService(f.store, RunArtifactResolver(f.root/'runs')), commands=None,
            artifacts=RunArtifactResolver(f.root/'runs')), runs_root=f.root/'runs', source_root=f.root/'source')
        app=local_ui.create_app(agent_api=agent, formal_generation_service=generation, formal_script_service=scripts,
            formal_brief_adapter=f.adapter, formal_brief_upload_factory=uploaded_document)
        page, origin=self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + '/projects/' + f.first['project_id'] + '/materials')
        expect(page.locator('#formal-image-synchronize')).to_be_visible()
        expect(page.locator('#formal-image-gate-copy')).to_contain_text('2 处图片来源尚未接入')
        with f.store.connection() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM project_images').fetchone()[0], 0)
        page.reload()
        expect(page.locator('#formal-image-synchronize')).to_be_visible()
        with f.store.connection() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM project_images').fetchone()[0], 0)
        page.locator('#formal-image-synchronize').click()
        expect(page.locator('#formal-image-synchronize')).to_be_hidden()
        with f.store.connection() as c: self.assertEqual(c.execute('SELECT COUNT(*) FROM project_images').fetchone()[0], 1)
        page.reload()
        expect(page.locator('#formal-image-pending')).to_have_text('1')
        expect(page.locator('#formal-image-synchronize')).to_be_hidden()
