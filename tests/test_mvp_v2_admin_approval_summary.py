import tempfile
import unittest
from pathlib import Path
from PIL import Image
from tests.test_mvp_v2_ui_workbench import UiWorkbenchFixture
from mvp_v2.formal_admin_service import FormalAdminService
from mvp_v2.knowledge_service import KnowledgeService

class AdminApprovalSummaryTests(unittest.TestCase):
    def test_summary_reads_real_asset_and_knowledge_pending_without_session_or_approval_writes(self):
        with tempfile.TemporaryDirectory() as root:
            f = UiWorkbenchFixture(Path(root))
            self.addCleanup(f.client.close)
            image = Path(root) / 'anonymous.png'
            Image.new('RGB', (32, 32), 'red').save(image)
            frozen = f.workbench.freeze_asset(f.auth_token, agent_session_id=f.session_id, source=str(image))
            params = dict(package_id=f.package_id, expected_active_package_version=f.package_version, idempotency_key='summary-import-01', category_id='launch', scope='project', file_refs=[frozen['artifact_ref']])
            prepared = f.workbench.prepare_asset_action(f.auth_token, agent_session_id=f.session_id, tool_name='create_asset_import', parameters=params)
            created = f.workbench.execute_asset_action(f.auth_token, agent_session_id=f.session_id, tool_name='create_asset_import', parameters=params, confirmation_token=prepared['confirmation_token'])
            self.assertEqual(created['status'], 'completed')
            k = KnowledgeService(f.store)
            state = k.get_state(f.auth_token, f.workspace_id, project_id=f.project['project_id'])
            candidate = k.create_candidate(f.auth_token, workspace_id=f.workspace_id, project_id=f.project['project_id'], scope='project', target_id=f.project['project_id'], statement='匿名规则建议', evidence_refs=['artifact://anonymous/fixture'], expected_knowledge_version=state['knowledge_version'])
            admin = FormalAdminService(f.store)
            with f.store.connection() as connection:
                before = {t: connection.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ['agent_sessions','tasks','knowledge_approvals','manual_command_audits']}
            result = admin.approval_summary(f.auth_token, workspace_id=f.workspace_id, workbench=f.workbench)
            self.assertEqual({item['object_type'] for item in result['items']}, {'asset', 'knowledge'})
            self.assertEqual(result['count'], 2)
            self.assertEqual(result['unavailable_sources'], [])
            for item in result['items']:
                self.assertIn(f.project['project_id'], item['href'])
            with f.store.connection() as connection:
                after = {t: connection.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in before}
            self.assertEqual(before, after)
            self.assertEqual(k.list_effective_rules(f.auth_token, f.project['project_id'])['rules'], [])
