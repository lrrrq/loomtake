"""Unknown image result through actual browser, HTTP, services and isolated SQLite."""
from copy import deepcopy
import unittest
from playwright.sync_api import expect
from mvp_v2.image_providers import ImageProviderError
from mvp_v2.local_ui import create_app
from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from tests import test_mvp_v2_image_creation_api as image_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class ImageCreationRecoveryBrowserTests(unittest.TestCase):
    def test_adopted_formal_image_refreshes_previously_opened_materials_without_new_version(self):
        from tests import test_mvp_v2_generated_image_delivery as delivery_fixture
        from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
        from mvp_v2.identity_service import LocalIdentityAdapter
        from mvp_v2.task_service import TaskService
        from mvp_v2.version_service import VersionService
        f = delivery_fixture.GeneratedImageDeliveryTests('runTest')
        f.setUp(); self.addCleanup(f.doCleanups)
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=f.f.store,
            identity=LocalIdentityAdapter(f.f.store), projects=f.f.projects,
            tasks=TaskService(f.f.store), versions=VersionService(f.f.store), artifacts=f.f.resolver))
        agent.script_revision = f.service.creative_revision
        app = create_app(agent_api=agent, formal_script_service=f.f.base.workspace,
            formal_image_service=f.f.workspace, formal_generation_service=f.service,
            formal_brief_adapter=FormalBriefInputAdapter(
                f.f.base.generated.briefs, data_root=f.f.base.generated.root/'runtime'),
            formal_brief_upload_factory=uploaded_document)
        page, origin = browser_fixture.ManualReceiptBrowserTests.open_page(
            self, app, f.token, f.f.workspace_id)
        route = origin+'/projects/'+f.f.project_id
        versions = deepcopy(f.f.projects.list_project_versions(f.token, f.f.project_id))
        page.goto(route+'/script?panel=images&aux=materials')
        expect(page.locator('[data-formal-image-key]')).to_have_count(36)
        page.get_by_role('link', name='创作', exact=True).click()
        expect(page.locator('[data-creative-create-image]')).to_be_visible()
        current = f.f.base.workspace.read_current(f.token, **f.scope)
        result = f.generate(current['shots'][0]['shot_id'], current['project_version'], 'materials-cache-01')
        page.locator('[data-creative-create-image]').click()
        adopt = page.locator(f'[data-creation-task="{result["task_id"]}"] [data-adopt-task]')
        expect(adopt).to_be_visible(); adopt.click()
        expect(adopt).to_have_text('已采用')
        page.get_by_role('button', name='关闭图片生成').click()
        page.get_by_role('link', name='图库与素材', exact=True).click()
        expect(page.locator('[data-formal-image-key]')).to_have_count(37)
        expect(page.get_by_role('button', name='分析图片', exact=True)).to_be_enabled()
        self.assertEqual(f.f.projects.list_project_versions(f.token, f.f.project_id), versions)
        self.assertEqual(len(f.calls), 1)

    def test_lost_receipt_unknown_result_reads_original_task_and_preserves_old_reference(self):
        f = image_fixture.ImageCreationApiTests('runTest'); f.setUp(); self.addCleanup(f.doCleanups)
        first = f.wait(f.start(f.payload('anonymous-prior-image')))
        adopted = f.f.client.post(f.path+'/'+first['task_id']+'/adopt', params=f.params,
            headers=f.f.headers, json=f.adoption(f.context()))
        self.assertEqual(adopted.status_code, 200, adopted.text)
        before = f.ensure_draft(); old_references = deepcopy(before['image_references'])
        self.assertEqual(len(old_references), 1)
        unknown_calls = []
        def uncertain_provider(*args, **kwargs):
            unknown_calls.append(1)
            raise ImageProviderError('IMAGE_TRANSPORT_FAILED', 'anonymous response lost',
                                     request_may_have_succeeded=True)
        f.f.generation.image_provider = uncertain_provider
        artifacts = RunArtifactResolver(f.f.root/'runs')
        # This existing creative draft has no formal version or review action.
        # Its empty-script read still uses the canonical workspace service.
        script = FormalScriptWorkspaceService(FormalScriptWorkspaceServices(
            store=f.f.store, projects=f.f.projects,
            delivery=VersionDeliveryService(f.f.store, artifacts), commands=None, artifacts=artifacts),
            runs_root=f.f.root/'runs', source_root=f.f.root/'source')
        app = create_app(agent_api=f.f.agent, formal_script_service=script,
            formal_generation_service=f.f.generation, formal_brief_adapter=f.f.adapter,
            formal_brief_upload_factory=uploaded_document)
        page, origin = browser_fixture.ManualReceiptBrowserTests.open_page(
            self, app, f.f.token, f.f.workspace_id)
        page.goto(origin+'/projects/'+f.base.project['project_id']+'/script?creative=draft')
        target = before['blocks'][1]['block_id']
        page.locator(f'[data-first-image="{target}"]').click()
        expect(page.locator('[data-creation-prompt]')).to_be_visible()
        page.locator('[data-creation-prompt]').fill('匿名请求：为当前段落生成另一张参考图')
        requests = []; tasks = []
        def lose_response(route):
            requests.append(route.request.post_data_json)
            response = route.fetch()
            self.assertEqual(response.status, 200, response.text())
            tasks.append(response.json()['task_id'])
            if len(requests) == 1: route.abort('connectionreset')
            else: route.fulfill(response=response)
        page.route('**/image-creations?*', lose_response)
        page.locator('[data-creation-generate]').click()
        expect(page.locator('[data-creation-retry]')).to_be_visible()
        page.locator('[data-creation-retry]').click()
        expect(page.locator('[data-creation-results]')).to_contain_text('结果待核实')
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1]); self.assertEqual(tasks[0], tasks[1])
        expect(page.locator('[data-creation-generate]')).to_be_disabled()
        expect(page.locator('[data-creation-retry]')).to_be_hidden()
        for _ in range(2):
            with page.expect_response(lambda r: tasks[0] in r.url and r.request.method=='GET') as read:
                page.locator(f'[data-creation-task="{tasks[0]}"] [data-refresh-task]').click()
            self.assertEqual(read.value.status, 200, read.value.text())
            self.assertTrue(read.value.json()['result_uncertain'])
            expect(page.locator('[data-creation-results]')).to_contain_text('结果待核实')
        page.get_by_role('button', name='关闭图片生成').click()
        page.reload(); page.locator(f'[data-first-image="{target}"]').click()
        expect(page.locator('[data-creation-results]')).to_contain_text('结果待核实')
        self.assertEqual(unknown_calls, [1])
        self.assertEqual(len(requests), 2)
        self.assertEqual(f.ensure_draft()['image_references'], old_references)
        self.assertEqual(f.ensure_draft()['blocks'], before['blocks'])
        self.assertEqual(f.f._table_count('project_versions'), 0)
        self.assertEqual(f.f._table_count('version_review_human_decisions'), 0)

    def test_formal_shot_unknown_result_preserves_frozen_images_and_versions(self):
        from tests import test_mvp_v2_generated_image_delivery as delivery_fixture
        from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
        from mvp_v2.identity_service import LocalIdentityAdapter
        from mvp_v2.task_service import TaskService
        from mvp_v2.version_service import VersionService
        f = delivery_fixture.GeneratedImageDeliveryTests('runTest')
        f.setUp(); self.addCleanup(f.doCleanups)
        before = f.f.workspace.read_current(f.token, **f.scope)
        self.assertGreater(len(before['shots']), 3)
        self.assertTrue(all(row['selected_image_key'] for row in before['shots']))
        versions = deepcopy(f.f.projects.list_project_versions(f.token, f.f.project_id))
        progress = deepcopy(f.f.workspace.read_progress(f.token, **f.scope))
        calls = []
        def uncertain_provider(*args, **kwargs):
            calls.append(1)
            raise ImageProviderError('IMAGE_TRANSPORT_FAILED', 'anonymous response lost',
                                     request_may_have_succeeded=True)
        f.service.image_provider = uncertain_provider
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=f.f.store,
            identity=LocalIdentityAdapter(f.f.store), projects=f.f.projects,
            tasks=TaskService(f.f.store), versions=VersionService(f.f.store), artifacts=f.f.resolver))
        agent.script_revision = f.service.creative_revision
        app = create_app(agent_api=agent, formal_script_service=f.f.base.workspace,
            formal_image_service=f.f.workspace, formal_generation_service=f.service,
            formal_brief_adapter=FormalBriefInputAdapter(
                f.f.base.generated.briefs, data_root=f.f.base.generated.root/'runtime'),
            formal_brief_upload_factory=uploaded_document)
        page, origin = browser_fixture.ManualReceiptBrowserTests.open_page(
            self, app, f.token, f.f.workspace_id)
        page.goto(origin+'/projects/'+f.f.project_id+'/script')
        page.locator('[data-creative-create-image]').click()
        page.locator('[data-creation-prompt]').fill('为当前镜头生成匿名参考图')
        expect(page.locator('[data-creation-availability]')).not_to_contain_text('正在读取')
        self.assertTrue(page.locator('[data-creation-generate]').is_enabled(), page.locator('[data-creation-availability]').inner_text())
        requests = []; tasks = []
        def lose_response(route):
            requests.append(route.request.post_data_json)
            response = route.fetch()
            self.assertEqual(response.status, 200, response.text())
            tasks.append(response.json()['task_id'])
            if len(requests) == 1: route.abort('connectionreset')
            else: route.fulfill(response=response)
        page.route('**/image-creations?*', lose_response)
        page.locator('[data-creation-generate]').click()
        expect(page.locator('[data-creation-retry]')).to_be_visible()
        page.locator('[data-creation-retry]').click()
        expect(page.locator('[data-creation-results]')).to_contain_text('结果待核实')
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1]); self.assertEqual(tasks[0], tasks[1])
        self.assertEqual(requests[0]['target_kind'], 'formal_shot')
        expect(page.locator('[data-creation-generate]')).to_be_disabled()
        for _ in range(2):
            with page.expect_response(lambda r: tasks[0] in r.url and r.request.method == 'GET') as read:
                page.locator(f'[data-creation-task="{tasks[0]}"] [data-refresh-task]').click()
            self.assertEqual(read.value.status, 200)
            self.assertTrue(read.value.json()['result_uncertain'])
        page.get_by_role('button', name='关闭图片生成').click()
        page.reload(); page.locator('[data-creative-create-image]').click()
        expect(page.locator('[data-creation-results]')).to_contain_text('结果待核实')
        self.assertEqual(calls, [1]); self.assertEqual(len(requests), 2)
        self.assertEqual(f.f.workspace.read_current(f.token, **f.scope)['shots'], before['shots'])
        self.assertEqual(f.f.workspace.read_progress(f.token, **f.scope), progress)
        self.assertEqual(f.f.projects.list_project_versions(f.token, f.f.project_id), versions)
