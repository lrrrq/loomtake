import unittest
from tests import test_mvp_v2_formal_revision_union as fixture

class TaskActionContextTests(unittest.TestCase):
    setUp = fixture.FormalRevisionUnionTests.setUp

    def test_context_uses_registered_project_version_and_stable_shots(self):
        before = self.base.workspace.read_current(self.token, workspace_id=self.workspace_id, project_id=self.project_id)
        result = self.client.post(f'/api/formal/projects/{self.project_id}/script/draft-proposals?workspace_id={self.workspace_id}', headers=self.headers, json=dict(expected_project_version=before['project_version'], expected_revision=before['revision'], instruction='把第2镜字幕修改为匿名开场', idempotency_key='context-proposal', entrypoint='assistant', feedback_kind='copy'))
        self.assertEqual(result.status_code, 200, result.text)
        task_id = result.json()['task_id']
        context = self.client.get(f'/api/formal/projects/{self.project_id}/tasks/{task_id}/context', headers=self.headers)
        self.assertEqual(context.status_code, 200, context.text)
        body = context.json()
        self.assertEqual(body['base_version_id'], before['base_version_id'])
        self.assertEqual(body['shots'][0]['shot_id'], before['shots'][1]['shot_id'])
        self.assertEqual(body['shots'][0]['shot_no'], before['shots'][1]['shot_no'])
        self.assertNotIn('internal_formal_parameters', context.text)
        self.assertNotIn('匿名开场', context.text)
        ws = self.projects.get_workspace(self.token, self.workspace_id)
        other = self.projects.create_project(self.token, workspace_id=self.workspace_id, category_id='launch', name='其他项目', expected_workspace_version=ws['workspace_version'])
        response = self.client.get(f"/api/formal/projects/{other['project_id']}/tasks/{task_id}/context", headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.base.workspace.read_current(self.token, workspace_id=self.workspace_id, project_id=self.project_id)['shots'], before['shots'])
