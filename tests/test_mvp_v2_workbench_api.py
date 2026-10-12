"""HTTP contracts for persisted workbench proposals; only models are fixtures."""
import unittest

from fastapi.testclient import TestClient

from mvp_v2.local_ui import create_app
from mvp_v2.formal_runtime import uploaded_document
from tests import test_mvp_v2_creative_workbench as workbench_fixture


class WorkbenchApiTests(unittest.TestCase):
    def setUp(self):
        self.f = workbench_fixture.CreativeWorkbenchTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.client = TestClient(create_app(agent_api=self.f.agent,
            formal_generation_service=self.f.generation, formal_image_service=self.f.image,
            formal_brief_adapter=self.f.generation.brief_input_adapter,
            formal_brief_upload_factory=uploaded_document))
        self.addCleanup(self.client.close)
        self.headers = {'X-MPlus-Agent-Auth': self.f.token,
                        'X-MPlus-UI-Token': self.client.app.state.session_token}
        self.url = f"/api/agent/sessions/{self.f.session['agent_session_id']}/messages"

    def payload(self):
        return {'content': '修改选中两段画面与声音，并按新画面推荐配图。',
                'context': self.f.context(), 'operation_key': 'http-existing-mixed-proposal'}

    def test_async_contract_refresh_confirm_replay_undo_and_session_timeline(self):
        before, payload = self.f.business(), self.payload()
        started = self.client.post(self.url, headers=self.headers, json=payload)
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()['response']['type'], 'workbench_progress')
        task_id = started.json()['task']['task_id']
        url = f'/api/agent/workbench/{task_id}'
        proposal = self.client.get(url, headers=self.headers)
        self.assertEqual(proposal.status_code, 200, proposal.text)
        proposal = proposal.json()
        self.assertEqual(proposal['response']['type'], 'workbench_proposal', proposal)
        self.assertEqual(self.f.business(), before)
        replay = self.client.post(self.url, headers=self.headers, json=payload)
        self.assertEqual(replay.json()['task']['task_id'], task_id)
        self.assertEqual(len(self.f.provider.advisor_inputs), 1)
        self.assertEqual(len(self.f.provider.editor_inputs), 1)
        preview_session = self.client.get(self.url.removesuffix('/messages'), headers=self.headers).json()
        children = [item for item in preview_session['tasks'] if item.get('workbench_parent_task_id') == task_id]
        self.assertEqual({item['task_type'] for item in children}, {'formal_creative_draft', 'formal_image_candidates'})
        child_ids = {item['task_id'] for item in children}
        self.assertTrue(all(item.get('workbench_parent_task_id') == task_id
                            for item in preview_session['timeline'] if item.get('task_id') in child_ids))
        project_url = f"/api/formal/projects/{self.f.scope['project_id']}"
        params = {'workspace_id': self.f.scope['workspace_id']}
        draft = self.client.get(project_url + '/creative-draft', headers=self.headers, params=params)
        self.assertEqual(draft.status_code, 200, draft.text)
        self.assertEqual(draft.json()['proposal']['workbench_parent_task_id'], task_id)
        text_child = next(item for item in children if item['task_type'] == 'formal_creative_draft')
        bypass = self.client.post(project_url + '/creative-draft/revisions/confirm', headers=self.headers,
            params=params, json={'task_id': text_child['task_id'],
                'proposal_digest': draft.json()['proposal']['digest'], 'retained_block_ids': ['block_1']})
        self.assertEqual(bypass.status_code, 409, bypass.text)
        self.assertEqual(bypass.json()['detail']['code'], 'WORKBENCH_CONFIRMATION_REQUIRED')
        image_child = next(item for item in children if item['task_type'] == 'formal_image_candidates')
        bypass = self.client.post(project_url + '/image-workspace/assistant/' + image_child['task_id'] + '/confirm',
            headers=self.headers, params=params,
            json={'proposal_digest': '0' * 64, 'selected_candidate_keys': ['untrusted-child-choice']})
        self.assertEqual(bypass.status_code, 409, bypass.text)
        self.assertEqual(bypass.json()['detail']['code'], 'WORKBENCH_CONFIRMATION_REQUIRED')
        self.assertEqual(self.f.business(), before)
        selection = {'proposal_digest': proposal['response']['proposal_digest'],
                     'retained_group_ids': ['block_1']}
        applied = self.client.post(url + '/confirm', headers=self.headers, json=selection)
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()['response']['type'], 'workbench_applied', applied.text)
        self.assertNotEqual(self.f.business(), before)
        again = self.client.post(url + '/confirm', headers=self.headers, json=selection)
        self.assertEqual(again.json()['response'], applied.json()['response'])
        self.assertEqual(again.json()['task']['task_id'], applied.json()['task']['task_id'])
        timeline = self.client.get(self.url.removesuffix('/messages'), headers=self.headers)
        self.assertEqual(timeline.status_code, 200, timeline.text)
        parent = [item for item in timeline.json()['timeline'] if item.get('task_id') == task_id]
        self.assertEqual(parent[-1]['response']['type'], 'workbench_applied')
        undone = self.client.post(url + '/undo', headers=self.headers, json={})
        self.assertEqual(undone.status_code, 200, undone.text)
        restored = self.f.business()
        self.assertEqual(restored['refs'], before['refs'])
        for original, current in zip(before['shots'], restored['shots']):
            # Restoring visual text is another edit through the authoritative
            # review core, which conservatively retains its review warning.
            self.assertEqual({k: v for k, v in current.items() if k != 'image_reconfirmation_required'},
                             {k: v for k, v in original.items() if k != 'image_reconfirmation_required'})
        self.assertTrue(restored['shots'][0]['image_reconfirmation_required'])
        self.assertTrue(self.client.get(url, headers=self.headers).json()['response']['undone'])

    def test_invalid_identity_untrusted_context_and_stale_revision_do_not_start_models(self):
        payload = self.payload()
        response = self.client.post(self.url, headers={'X-MPlus-UI-Token': self.headers['X-MPlus-UI-Token']}, json=payload)
        self.assertIn(response.status_code, (401, 403))
        response = self.client.post(self.url, headers={'X-MPlus-Agent-Auth': self.f.token}, json=payload)
        self.assertEqual(response.status_code, 403)
        payload['context']['proposal_rows'] = [{'id': 'foreign-target'}]
        self.assertEqual(self.client.post(self.url, headers=self.headers, json=payload).status_code, 422)
        payload = self.payload()
        payload['context']['expected_revision'] -= 1
        response = self.client.post(self.url, headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(self.f.provider.advisor_inputs)
        self.assertFalse(self.f.provider.editor_inputs)


if __name__ == '__main__':
    unittest.main()
