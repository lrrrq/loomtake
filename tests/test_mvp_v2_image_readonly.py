"""RV06-05: image GET is read-only; old confirmed sources have an explicit action."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import unittest
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.formal_generation_service import FormalGenerationService, FormalGenerationServices
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.knowledge_service import KnowledgeService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_project_image_authority as fixture


class ImageReadonlyTests(unittest.TestCase):
    def test_viewer_reads_old_sources_without_writes_owner_explicitly_materializes(self):
        f = fixture.ProjectImageAuthorityTests('runTest'); f.setUp(); self.addCleanup(f.tearDown)
        f._confirmed_first()
        service = FormalGenerationService(FormalGenerationServices(briefs=f.briefs,
            projects=f.projects, tasks=TaskService(f.store), artifacts=RunArtifactResolver(f.root/'runs'),
            images=f.pool, knowledge=KnowledgeService(f.store)), runs_root=f.root/'runs', source_root=f.root/'source')
        identity=LocalIdentityAdapter(f.store)
        actor=identity.create_actor(f.token, workspace_id=f.workspace_id, project_id=f.first['project_id'],
            display_name='匿名查看者', auth_source='local', role='viewer',
            expected_workspace_version=f.projects.get_workspace(f.token, f.workspace_id)['workspace_version'])
        viewer=identity.issue_auth_session(actor_id=actor['actor_id'], token='image-readonly-viewer')['auth_token']
        scope=dict(workspace_id=f.workspace_id, project_id=f.first['project_id'],
            expected_project_version=f.projects.get_project(f.token, f.first['project_id'])['project_version'])
        def snapshot():
            with f.store.connection() as c:
                return list(c.iterdump())
        before=snapshot()
        first=service.get_image_readiness(viewer, **scope)
        self.assertEqual(first['unmaterialized_source_count'], 2)
        self.assertEqual(first['total'], 0)
        self.assertEqual(service.get_image_readiness(viewer, **scope), first)
        self.assertEqual(snapshot(), before)
        with self.assertRaises(WorkspaceServiceError): service.synchronize_image_sources(viewer, **scope)
        ready=service.synchronize_image_sources(f.token, **scope)
        self.assertEqual(ready['total'], 1)
        self.assertEqual(ready['unmaterialized_source_count'], 0)
        self.assertEqual(service.synchronize_image_sources(f.token, **scope), ready)
        before=snapshot()
        self.assertEqual(service.get_image_readiness(viewer, **scope), ready)
        self.assertEqual(snapshot(), before)
        # A concurrent save owns SQLite's writer slot; browsing must not need it.
        with ThreadPoolExecutor(max_workers=1) as pool:
            with f.store.connection() as writer:
                writer.execute('BEGIN IMMEDIATE')
                try:
                    pending = pool.submit(service.get_image_readiness, viewer, **scope)
                    self.assertEqual(pending.result(timeout=3), ready)
                finally:
                    writer.rollback()
        self.assertEqual(snapshot(), before)
        frozen = next(path for path in (f.root / 'data' / 'artifacts' / 'formal-images').rglob('*')
                      if path.is_file())
        frozen.write_bytes(b'tampered readonly fixture')
        with self.assertRaisesRegex(WorkspaceServiceError, 'FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED') as refused:
            service.get_image_readiness(viewer, **scope)
        self.assertEqual(refused.exception.__cause__.code, 'IMAGE_ARTIFACT_DRIFT')
        self.assertEqual(snapshot(), before)
