import unittest
from tests import test_mvp_v2_formal_admin_service as fixture
from mvp_v2.formal_admin_service import FormalAdminService

class ProjectProgressSummaryTests(unittest.TestCase):
    setUp = fixture.FormalAdminServiceTests.setUp
    tearDown = fixture.FormalAdminServiceTests.tearDown

    def test_registered_task_and_unknown_stage_remain_separate_without_artifact_loads(self):
        workspace = self.projects.get_workspace(self.owner_token, self.workspace_id)
        new = self.projects.create_project(self.owner_token, workspace_id=self.workspace_id, name='仅名称项目', expected_workspace_version=workspace['workspace_version'])
        result = self.projects.list_progress_summaries(self.owner_token, self.workspace_id)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[new['project_id']]['stage'], '尚未导入需求')
        self.assertIn('/brief', result[new['project_id']]['next_action']['href'])
        self.assertTrue(result[self.project['project_id']]['next_action']['href'].startswith('/projects/' + self.project['project_id']))
        fallback = self.projects.progress_projection(new)
        self.assertEqual(fallback['next_action']['label'], '打开项目')
        self.assertEqual(fallback['stage'], '状态待核对')
        self.assertEqual(self.projects.progress_projection({**new,'status':'archived'})['next_action']['label'], '查看／恢复')
