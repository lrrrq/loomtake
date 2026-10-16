"""Anonymous snapshot relocation with a prior image and an uncertain request."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from playwright.sync_api import expect
from openpyxl import load_workbook

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.cloud_migration import create_cloud_snapshot, restore_cloud_snapshot
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_generation_service import FormalGenerationService, FormalGenerationServices
from mvp_v2.formal_image_workspace import FormalImageWorkspaceService, FormalImageWorkspaceServices
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.image_providers import ImageProviderError
from mvp_v2.knowledge_service import KnowledgeService
from mvp_v2.local_ui import create_app
from mvp_v2.project_image_authority import ProjectImagePoolService
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceStore
from tests import test_mvp_v2_image_creation as image_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture
from tests import test_mvp_v2_cloud_migration as migration_fixture


class CloudRestoreBrowserTests(unittest.TestCase):
    def test_relocated_snapshot_preserves_parent_child_artifacts_and_package(self):
        temporary = tempfile.TemporaryDirectory(prefix='mplus-version-restore-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        f = migration_fixture.CloudMigrationTests('runTest')._data_fixture(root)
        data, token = f['data_root'], f['token']
        projects = ProjectService(WorkspaceStore(data / 'workspace.sqlite3'))
        project_id = f['project']['project_id']
        old_run = data / 'runs' / project_id / 'runs' / f['run_id']
        child_id = 'run_portable_002'
        child_run = old_run.parent / child_id
        shutil.copytree(old_run, child_run)
        child_book = child_run / 'delivery.xlsx'
        book = load_workbook(child_book)
        book['脚本']['A1'] = '匿名恢复子版本'
        book.save(child_book)
        book.close()
        manifest_path = child_run / 'run_manifest.json'
        manifest = json.loads(manifest_path.read_text().replace(str(old_run), str(child_run)))
        manifest['run_id'] = child_id
        for item in manifest['artifacts'].values():
            artifact = Path(item['path'])
            item.update(sha256=sha256(artifact.read_bytes()).hexdigest(), size_bytes=artifact.stat().st_size)
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        bound = projects.bind_run(token, project_id=project_id, run_id=child_id,
            source_kind='native', manifest_ref=f'artifact://run/{child_id}/manifest',
            manifest_sha256=sha256(manifest_path.read_bytes()).hexdigest(),
            expected_project_version=projects.get_project(token, project_id)['project_version'])
        child = projects.create_project_version(token, project_id=project_id, run_id=child_id,
            content_sha256=sha256(child_book.read_bytes()).hexdigest(),
            artifact_ref=f'artifact://run/{child_id}/delivery_workbook',
            expected_project_version=bound['project_version'], parent_version_id=f['version']['version_id'])
        versions = projects.list_project_versions(token, project_id)
        source_hashes = {str(p.relative_to(data)): sha256(p.read_bytes()).hexdigest()
            for folder in ('runs', 'packages', 'sources') for p in (data / folder).rglob('*') if p.is_file()}
        archive = root / 'versions.zip'
        create_cloud_snapshot(data, archive, source_root=f['source_root'],
            asset_packages=(f['package'],), source_commit=migration_fixture.SOURCE_COMMIT)
        data.rename(root / 'offline-original')
        restored = root / 'restored'
        result = restore_cloud_snapshot(archive, restored, source_commit=migration_fixture.SOURCE_COMMIT)
        self.assertEqual(result['bound_runs']['verified'], 2)
        recovered = ProjectService(WorkspaceStore(restored / 'workspace.sqlite3'))
        self.assertEqual(recovered.list_project_versions(token, project_id), versions)
        self.assertEqual(recovered.get_project(token, project_id)['current_version_id'], child['version_id'])
        for relative, digest in source_hashes.items():
            self.assertEqual(sha256((restored / relative).read_bytes()).hexdigest(), digest, relative)
        resolver = RunArtifactResolver(restored / 'runs')
        for run_id, text in ((f['run_id'], '匿名云迁移交付'), (child_id, '匿名恢复子版本')):
            workbook = resolver.resolve_artifact(run_id, 'delivery_workbook').path
            book = load_workbook(workbook)
            self.assertEqual(book['脚本']['A1'].value, text)
            book.close()
        self.assertFalse(data.exists())

    def test_relocated_snapshot_keeps_image_and_unknown_request_without_regeneration(self):
        f = image_fixture.ImageCreationTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        first = f.execute(f.start('restore-prior-image'))
        image_bytes, _ = f.service.preview_image_creation(f.token, **f.scope, task_id=first['task_id'])
        current = f.current()
        target = current['blocks'][1]['block_id']
        f.service.adopt_image_creation(f.token, **f.scope, task_id=first['task_id'],
            target_kind='creative_block', target_id=target,
            expected_project_version=current['project_version'],
            expected_creative_revision=current['revision'], operation_key='restore-prior-adoption')
        calls = []

        def uncertain_provider(*args, **kwargs):
            calls.append(1)
            raise ImageProviderError('IMAGE_TRANSPORT_FAILED', 'anonymous lost response',
                                     request_may_have_succeeded=True)

        f.service.image_provider = uncertain_provider
        pending = f.execute(f.start('restore-unknown-image'))
        self.assertTrue(pending['result_uncertain'])
        before = f.current()
        self.assertEqual(len(before['blocks']), 3)
        self.assertEqual(len(before['image_references']), 1)
        root = f.base.fixture.root
        data = root / 'runtime'
        shutil.copytree(f.base.fixture.source_root, data / 'sources')
        commit = 'a' * 40  # Anonymous application identity, never a deployed SHA claim.
        archive = root / 'backup.zip'
        create_cloud_snapshot(data, archive, source_root=data / 'sources', source_commit=commit)
        # Make the original unavailable: no restored read may silently use old paths.
        offline = root / 'offline-original'
        data.rename(offline)
        restored = root / 'restored'
        started = time.monotonic()
        restore_cloud_snapshot(archive, restored, source_commit=commit)
        restore_seconds = time.monotonic() - started

        store = WorkspaceStore(restored / 'workspace.sqlite3')
        projects, tasks = ProjectService(store), TaskService(store)
        artifacts = RunArtifactResolver(restored / 'runs')
        briefs = FormalBriefWorkspaceService(store, artifact_root=restored)
        service = FormalGenerationService(FormalGenerationServices(
            briefs=briefs, projects=projects, tasks=tasks, artifacts=artifacts,
            images=ProjectImagePoolService(store, data_root=restored),
            knowledge=KnowledgeService(store)), runs_root=restored / 'runs',
            source_root=restored / 'sources')
        service.image_provider = uncertain_provider
        adapter = FormalBriefInputAdapter(briefs, data_root=restored)
        service.brief_input_adapter = adapter
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=store,
            identity=LocalIdentityAdapter(store), projects=projects, tasks=tasks,
            versions=VersionService(store), artifacts=artifacts))
        script = FormalScriptWorkspaceService(FormalScriptWorkspaceServices(
            store=store, projects=projects,
            delivery=VersionDeliveryService(store, artifacts), commands=None, artifacts=artifacts),
            runs_root=restored / 'runs', source_root=restored / 'sources')
        images = FormalImageWorkspaceService(FormalImageWorkspaceServices(
            store=store, projects=projects, delivery=VersionDeliveryService(store, artifacts),
            artifacts=artifacts, images=service.services.images),
            runs_root=restored / 'runs', source_root=restored / 'sources')
        revision = FormalRevisionService(projects=projects, tasks=tasks, workspace=script,
            image_workspace=images, brief_adapter=adapter, writes_enabled=True)
        agent.script_revision = revision
        service.creative_revision = revision
        app = create_app(agent_api=agent, formal_script_service=script,
            formal_image_service=images,
            formal_generation_service=service, formal_brief_adapter=adapter,
            formal_brief_upload_factory=uploaded_document)
        page, origin = browser_fixture.ManualReceiptBrowserTests.open_page(
            self, app, f.token, f.scope['workspace_id'])
        failures = []
        page.on('response', lambda r: failures.append((r.status, r.url))
                if '/api/' in r.url and r.status >= 400 else None)
        page.goto(origin + '/projects/' + f.scope['project_id'] + '/script?creative=draft')
        for _ in range(2):
            page.locator('[data-first-view="overview"]').click()
            page.locator(f'[data-workbench-open="{target}"]').click()
            page.locator(f'[data-first-image="{target}"]').click()
            card = page.locator(f'[data-creation-task="{pending["task_id"]}"]')
            expect(card).to_contain_text('结果待核实')
            with page.expect_response(lambda r: pending['task_id'] in r.url and r.request.method == 'GET') as read:
                card.locator('[data-refresh-task]').click()
            self.assertEqual(read.value.status, 200)
            self.assertTrue(read.value.json()['result_uncertain'])
            page.get_by_role('button', name='关闭图片生成').click()
            page.reload()
        self.assertEqual(failures, [])
        service.execute_image_creation(f.token, **f.scope, task_id=pending['task_id'])
        self.assertEqual(calls, [1])
        after = service.read_creative_draft(f.token, **f.scope)
        self.assertEqual(after['blocks'], before['blocks'])
        self.assertEqual(after['image_references'], before['image_references'])
        self.assertEqual(service.preview_image_creation(f.token, **f.scope,
            task_id=first['task_id'])[0], image_bytes)
        with store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM tasks WHERE task_type='formal_image_creation'").fetchone()[0], 2)
            self.assertEqual(connection.execute('SELECT count(*) FROM project_versions').fetchone()[0], 0)
        self.assertFalse(data.exists())
        print(json.dumps({'scope': 'anonymous_local_snapshot_browser',
            'restore_seconds': restore_seconds, 'provider_calls_after_recovery': len(calls),
            'image_sha256': sha256(image_bytes).hexdigest(),
            'production_rpo_rto_verified': False}, sort_keys=True))
