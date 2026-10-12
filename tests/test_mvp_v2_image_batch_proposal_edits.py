from __future__ import annotations

import unittest

from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_image_assistant_batch as fixture


class ImageBatchProposalEditsTests(unittest.TestCase):
    def setUp(self):
        self.base = fixture.ImageAssistantBatchTests('runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.f, self.service = self.base.f, self.base.revision
        self.scope = self.base.base.scope

    def progress(self):
        return self.f.workspace.read_progress(self.f.token, **self.scope)

    def test_revise_restart_partial_apply_and_stale_revision_preserve_original_progress(self):
        original = self.progress()
        proposal = self.base.propose(mode='replace_selected')
        self.assertEqual(proposal['task']['status'], 'waiting_user')
        shots = proposal['response']['shots']
        first, second = shots[:2]
        candidate = first['candidates'][-1]
        revised = self.service.revise_image_batch(self.f.token, task_id=proposal['task']['task_id'],
            proposal_digest=proposal['response']['proposal_digest'], updates=[
                {'shot_id': first['shot_id'], 'candidate_key': candidate['candidate_key']},
                {'shot_id': second['shot_id'], 'included': False}])
        self.assertEqual(revised['response']['proposal_revision'], 1)
        self.assertEqual(revised['task']['task_version'], proposal['task']['task_version'] + 1)
        self.assertEqual(self.progress(), original)
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.service.revise_image_batch(self.f.token, task_id=proposal['task']['task_id'],
                proposal_digest=proposal['response']['proposal_digest'], updates=[{'shot_id': first['shot_id'], 'included': False}])
        self.assertEqual(caught.exception.code, 'PROPOSAL_DIGEST_MISMATCH')
        restarted = FormalRevisionService(projects=self.f.projects, tasks=TaskService(self.f.store),
            workspace=self.f.base.workspace, image_workspace=self.f.workspace, writes_enabled=True)
        restored = restarted.read_image_batch(self.f.token, **self.scope)
        self.assertEqual(restored['response']['shots'], revised['response']['shots'])
        self.assertFalse(restored['response']['shots'][1]['included'])
        applied = restarted.confirm_image_batch(self.f.token, task_id=proposal['task']['task_id'],
            proposal_digest=revised['response']['proposal_digest'], selected_candidate_keys=[candidate['candidate_key']])
        self.assertEqual([s['shot_id'] for s in applied['progress']['selections']], [first['shot_id']])
        self.assertFalse(applied['progress']['selections'][0]['checked'])

    def test_picker_choice_stays_in_same_proposal_and_cancel_is_persistent(self):
        proposal = self.base.propose(mode='replace_selected')
        shot = proposal['response']['shots'][0]
        candidate = shot['candidates'][-1]
        updated = self.service.revise_image_batch(self.f.token, task_id=proposal['task']['task_id'],
            proposal_digest=proposal['response']['proposal_digest'],
            updates=[{'shot_id': shot['shot_id'], 'image_key': candidate['image_key'], 'query': '用户逐镜选择'}])
        self.assertEqual(updated['task']['task_id'], proposal['task']['task_id'])
        self.assertEqual(self.progress()['revision'], 0)
        cancelled = self.service.cancel_image_batch(self.f.token, task_id=proposal['task']['task_id'],
            proposal_digest=updated['response']['proposal_digest'])
        self.assertEqual(cancelled['task']['status'], 'cancelled')
        restored = self.service.read_image_batch(self.f.token, **self.scope, task_id=proposal['task']['task_id'])
        self.assertEqual(restored['response']['status'], 'cancelled')
        self.assertEqual(restored['response']['shots'], updated['response']['shots'])
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.service.confirm_image_batch(self.f.token, task_id=proposal['task']['task_id'],
                proposal_digest=updated['response']['proposal_digest'])
        self.assertEqual(caught.exception.code, 'INVALID_TASK_TRANSITION')
        self.assertEqual(self.progress()['revision'], 0)

    def test_explicit_replace_retrieves_manual_selection_and_preserves_locks(self):
        self.base.base.patch([self.base.base.selection(0)])
        target = self.base.base.current['shots'][1]['shot_id']
        self.f.base.workspace.set_assistant_locks(self.f.token, **self.scope,
            expected_project_version=self.base.base.current['project_version'], locked_shot_ids=[target],
            idempotency_key='lock-proposal-scope')
        proposal = self.base.propose('为所有镜头配图', mode='replace_selected')
        self.assertTrue(proposal['response']['shots'][0]['retrieval']['executed'])
        locked = next(s for s in proposal['response']['shots'] if s['shot_id'] == target)
        self.assertFalse(locked['retrieval']['executed'])
        self.assertEqual(locked['reason_code'], 'locked')
        self.assertEqual(self.progress()['revision'], 1)

    def test_http_refresh_and_updates_enforce_auth_and_project_scope(self):
        client, headers = self.base.http_client()
        proposal = self.base.propose()
        route = f"/api/formal/projects/{self.f.project_id}/image-workspace/assistant"
        params = {'workspace_id': self.f.workspace_id}
        self.assertEqual(client.get(route, params=params).status_code, 401)
        response = client.get(route, params=params, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['task']['task_id'], proposal['task']['task_id'])
        shot = proposal['response']['shots'][0]
        body = {'proposal_digest': proposal['response']['proposal_digest'],
                'updates': [{'shot_id': shot['shot_id'], 'included': False}]}
        revised = client.patch(route + '/' + proposal['task']['task_id'], params=params, headers=headers, json=body)
        self.assertEqual(revised.status_code, 200, revised.text)
        self.assertEqual(self.progress()['revision'], 0)
        self.assertFalse(revised.json()['response']['shots'][0]['included'])
