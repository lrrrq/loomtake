from __future__ import annotations

import unittest
from unittest.mock import patch

from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_image_progress as fixture_module
from tests import test_mvp_v2_formal_image_workspace as api_fixture


class ImageAssistantBatchTests(unittest.TestCase):
    def setUp(self):
        self.base = fixture_module.ImageProgressTests('runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.f = self.base.fixture
        self.revision = FormalRevisionService(
            projects=self.f.projects, tasks=TaskService(self.f.store),
            workspace=self.f.base.workspace, image_workspace=self.f.workspace,
            writes_enabled=True,
        )

    def propose(self, instruction='帮整份稿配一版图', **kwargs):
        return self.revision.propose_image_batch(
            self.f.token, **self.base.scope, instruction=instruction,
            expected_project_version=self.base.current['project_version'],
            idempotency_key='batch-proposal', **kwargs,
        )

    def http_client(self):
        agent = api_fixture.ReadonlyAgentService(api_fixture.ReadonlyAgentServices(
            store=self.f.store, identity=api_fixture.LocalIdentityAdapter(self.f.store),
            projects=self.f.projects, tasks=TaskService(self.f.store),
            versions=api_fixture.VersionService(self.f.store), artifacts=self.f.resolver,
        ))
        agent.script_revision = self.revision
        client = api_fixture.TestClient(api_fixture.create_app(
            agent_api=agent, formal_script_service=self.f.base.workspace,
            formal_image_service=self.f.workspace,
        ))
        self.addCleanup(client.close)
        return client, {'X-MPlus-Agent-Auth': self.f.token,
                        'X-MPlus-UI-Token': client.app.state.session_token}

    def test_library_first_saves_eligible_scope_and_preserves_manual_choices_without_review(self):
        manual = self.base.patch([self.base.selection(0)])
        ids = [self.base.current['shots'][i]['shot_id'] for i in (0, 2)]
        before = self.f.projects.list_project_versions(self.f.token, self.f.project_id)
        response = self.propose('为已选镜头配图', selected_shot_ids=ids, save_eligible=True)['response']
        progress = self.f.workspace.read_progress(self.f.token, **self.base.scope)
        self.assertEqual(response['saved_shot_ids'], [ids[1]])
        self.assertEqual({s['shot_id'] for s in progress['selections']}, set(ids))
        self.assertEqual(next(s for s in progress['selections'] if s['shot_id'] == ids[0]), manual['selections'][0])
        self.assertFalse(next(s for s in progress['selections'] if s['shot_id'] == ids[1])['checked'])
        self.assertEqual(self.f.projects.list_project_versions(self.f.token, self.f.project_id), before)

    def test_reference_only_candidates_remain_available_without_automatic_adoption(self):
        original = self.f.workspace.search_shot
        def references(*args, **kwargs):
            result = original(*args, **kwargs)
            result['candidates'] = [{**c, 'default_eligible': False, 'suggestion_eligible': False,
                                     'suggestion_kind': 'composition_reference'} for c in result['candidates']]
            return result
        target = self.base.current['shots'][2]['shot_id']
        with patch.object(self.f.workspace, 'search_shot', side_effect=references):
            response = self.propose('为已选镜头配图', selected_shot_ids=[target], save_eligible=True)['response']
        self.assertEqual(response['saved_shot_ids'], [])
        self.assertEqual(response['gap_shot_ids'], [])
        self.assertEqual(response['shots'][0]['retrieval']['eligible_candidates'], 0)
        self.assertEqual(response['shots'][0]['selected_candidate_key'], '')
        self.assertTrue(response['shots'][0]['candidates'])
        self.assertEqual(self.f.workspace.read_progress(self.f.token, **self.base.scope)['revision'], 0)

    def test_unrelated_sound_edit_does_not_invalidate_image_proposal(self):
        proposal = self.propose('第3镜换图')
        current = self.f.base.workspace.read_current(self.f.token, **self.base.scope)
        self.f.base.workspace.apply_table_change(self.f.token, **self.base.scope,
            expected_project_version=self.base.current['project_version'], expected_revision=current['revision'],
            shot_no='3', shot_id=current['shots'][2]['shot_id'], field='sound',
            value='仅调整声音，画面不变', idempotency_key='sound-between-image-actions')
        reread = self.revision.read_image_batch(self.f.token, **self.base.scope,
            task_id=proposal['task']['task_id'])
        self.assertFalse(reread['response']['basis_changed'])
        saved = self.revision.confirm_image_batch(self.f.token, task_id=proposal['task']['task_id'],
            proposal_digest=proposal['response']['proposal_digest'])
        self.assertEqual(saved['progress']['revision'], 1)

    def test_restore_old_project_revision_preserves_proposal_but_marks_basis_changed(self):
        proposal = self.propose('第3镜换图')
        before = self.revision.read_image_batch(self.f.token, **self.base.scope)
        self.assertFalse(before['response']['basis_changed'])
        self.f.projects.rename_project(self.f.token, project_id=self.f.project_id,
            name='匿名项目新名称', expected_project_version=self.base.current['project_version'])
        after = self.revision.read_image_batch(self.f.token, **self.base.scope)
        self.assertTrue(after['response']['basis_changed'])
        self.assertEqual(after['task'], before['task'])
        self.assertEqual(after['response']['shots'], before['response']['shots'])
        self.assertEqual(after['response']['proposal_digest'], proposal['response']['proposal_digest'])
        self.assertEqual(self.f.workspace.read_progress(self.f.token, **self.base.scope)['revision'], 0)

    def test_api_requires_auth_strict_body_and_explicit_in_scope_shots(self):
        client, headers = self.http_client()
        route = f'/api/formal/projects/{self.f.project_id}/image-workspace/assistant'
        query = {'workspace_id': self.f.workspace_id}
        body = {'expected_project_version': self.base.current['project_version'],
                'instruction': '第2镜换图', 'idempotency_key': 'api-proposal',
                'selected_shot_ids': [self.base.current['shots'][1]['shot_id']]}
        self.assertEqual(client.post(route, params=query, json=body).status_code, 401)
        self.assertEqual(client.post(route, params=query, headers=headers,
                                     json={**body, 'model': 'client-override'}).status_code, 422)
        for invalid in (
            {**body, 'selected_shot_ids': ['foreign-shot']},
            {**body, 'selected_shot_ids': body['selected_shot_ids'] * 2},
            {**body, 'instruction': '第1镜换图'},
            {**body, 'instruction': '第999镜换图'},
        ):
            with self.subTest(invalid=invalid):
                rejected = client.post(route, params=query, headers=headers, json=invalid)
                self.assertEqual(rejected.status_code, 409, rejected.text)
                self.assertEqual(rejected.json()['detail']['code'], 'IMAGE_SCOPE_MISMATCH')
        self.assertEqual(self.f.workspace.read_progress(self.f.token, **self.base.scope)['revision'], 0)
        accepted = client.post(route, params=query, headers=headers, json=body)
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()['response']['affected_shot_ids'], body['selected_shot_ids'])

    def test_api_rejects_foreign_project_task_and_candidate_then_replays_exact_save(self):
        client, headers = self.http_client()
        proposal = self.propose('第2镜换图')
        response = proposal['response']
        candidate = response['shots'][0]['candidates'][0]
        body = {'proposal_digest': response['proposal_digest'],
                'selected_candidate_keys': [candidate['candidate_key']]}
        tail = f"/image-workspace/assistant/{proposal['task']['task_id']}/confirm"
        route = f'/api/formal/projects/{self.f.project_id}' + tail
        query = {'workspace_id': self.f.workspace_id}
        workspace = self.f.projects.get_workspace(self.f.token, self.f.workspace_id)
        other = self.f.projects.create_project(self.f.token, workspace_id=self.f.workspace_id,
            name='另一个匿名项目', expected_workspace_version=workspace['workspace_version'])
        rejected = client.post(f"/api/formal/projects/{other['project_id']}" + tail,
            params=query, headers=headers, json=body)
        self.assertEqual(rejected.status_code, 403, rejected.text)
        self.assertEqual(rejected.json()['detail']['code'], 'CROSS_PROJECT_DENIED')
        for invalid in ({**body, 'selected_candidate_keys': ['foreign-candidate']},
                        {**body, 'selected_candidate_keys': body['selected_candidate_keys'] * 2},
                        {**body, 'selected_candidate_keys': [row['candidate_key'] for row in response['shots'][0]['candidates'][:2]]}):
            result = client.post(route, params=query, headers=headers, json=invalid)
            self.assertEqual(result.status_code, 409, result.text)
        before = self.f.projects.list_project_versions(self.f.token, self.f.project_id)
        saved = client.post(route, params=query, headers=headers, json=body)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(client.post(route, params=query, headers=headers, json=body).json(), saved.json())
        self.assertEqual(saved.json()['progress']['revision'], 1)
        self.assertFalse(saved.json()['progress']['selections'][0]['checked'])
        self.assertEqual(self.f.projects.list_project_versions(self.f.token, self.f.project_id), before)
        alternate = response['shots'][0]['candidates'][1]['candidate_key']
        changed = client.post(route, params=query, headers=headers,
            json={**body, 'selected_candidate_keys': [alternate]})
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(changed.json()['detail']['code'], 'IDEMPOTENCY_CONFLICT')

    def test_script_edit_after_proposal_keeps_batch_atomic(self):
        proposal = self.propose()
        script = self.f.base.workspace.read_current(self.f.token, **self.base.scope)
        self.f.base.workspace.apply_table_change(self.f.token, **self.base.scope,
            expected_project_version=self.base.current['project_version'], expected_revision=script['revision'],
            shot_no='2', shot_id=script['shots'][1]['shot_id'], field='visual',
            value=script['shots'][1]['visual'] + '，改为近景。', idempotency_key='change-after-batch')
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.revision.confirm_image_batch(self.f.token, task_id=proposal['task']['task_id'],
                proposal_digest=proposal['response']['proposal_digest'])
        self.assertEqual(caught.exception.code, 'DRAFT_REVISION_CONFLICT')
        self.assertEqual(self.f.workspace.read_progress(self.f.token, **self.base.scope)['revision'], 0)

    def test_deleted_target_cannot_transfer_old_proposal_to_renumbered_survivor(self):
        original = self.f.base.workspace.read_current(self.f.token, **self.base.scope)
        target = original['shots'][1]
        survivor = original['shots'][2]
        proposal = self.propose('重新推荐所选镜头的图片',
            selected_shot_ids=[target['shot_id']], mode='replace_selected')
        changed = self.f.base.workspace.apply_revision_operation(self.f.token, **self.base.scope,
            expected_project_version=original['project_version'], expected_revision=original['revision'],
            operation={'operation': 'delete_shot', 'shot_no': target['shot_no'], 'shot_id': target['shot_id']},
            idempotency_key='delete-target-after-proposal')
        self.assertEqual(changed['shots'][1]['shot_id'], survivor['shot_id'])
        before = self.f.workspace.read_progress(self.f.token, **self.base.scope)
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.revision.confirm_image_batch(self.f.token, task_id=proposal['task']['task_id'],
                proposal_digest=proposal['response']['proposal_digest'])
        self.assertEqual(caught.exception.code, 'DRAFT_REVISION_CONFLICT')
        self.assertEqual(self.f.workspace.read_progress(self.f.token, **self.base.scope), before)
        self.assertEqual(self.f.base.workspace.read_current(self.f.token, **self.base.scope)['shots'], changed['shots'])

    def test_proposal_blob_identity_drift_cannot_be_saved_as_original_candidate(self):
        proposal = self.propose('第2镜换图')
        chosen = proposal['response']['shots'][0]['candidates'][0]
        digest = chosen['content_sha256']
        target = self.f.base.generated.root / 'runtime' / 'artifacts' / 'formal-images' / digest[:2] / digest
        original = target.read_bytes()
        target.write_bytes(original + b'anonymous-blob-drift')
        try:
            with self.assertRaises(WorkspaceServiceError):
                self.revision.confirm_image_batch(self.f.token, task_id=proposal['task']['task_id'],
                    proposal_digest=proposal['response']['proposal_digest'],
                    selected_candidate_keys=[chosen['candidate_key']])
        finally:
            target.write_bytes(original)
        self.assertEqual(self.f.workspace.read_progress(self.f.token, **self.base.scope)['revision'], 0)

    def test_batch_preserves_manual_progress_and_stages_unchecked_once(self):
        chosen = self.base.selection(0)
        original = self.base.patch([chosen])
        before = self.f.projects.list_project_versions(self.f.token, self.f.project_id)
        proposal = self.propose()
        response = proposal['response']
        self.assertGreater(len(response['shots']), 2)
        self.assertNotIn(chosen['shot_id'], response['affected_shot_ids'])
        result = self.revision.confirm_image_batch(
            self.f.token, task_id=proposal['task']['task_id'], proposal_digest=response['proposal_digest'],
        )
        self.assertEqual(result['progress']['revision'], 2)
        by_id = {s['shot_id']: s for s in result['progress']['selections']}
        self.assertEqual(by_id[chosen['shot_id']], original['selections'][0])
        self.assertFalse(any(by_id[sid]['checked'] for sid in response['affected_shot_ids']))
        again = self.revision.confirm_image_batch(
            self.f.token, task_id=proposal['task']['task_id'], proposal_digest=response['proposal_digest'],
        )
        self.assertEqual(again, result)
        self.assertEqual(self.f.projects.list_project_versions(self.f.token, self.f.project_id), before)

    def test_manual_change_after_proposal_rejects_whole_batch(self):
        proposal = self.propose()
        original = self.base.patch([self.base.selection(1)])
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.revision.confirm_image_batch(
                self.f.token, task_id=proposal['task']['task_id'],
                proposal_digest=proposal['response']['proposal_digest'],
            )
        self.assertEqual(caught.exception.code, 'IMAGE_PROGRESS_CONFLICT')
        self.assertEqual(self.f.workspace.read_progress(self.f.token, **self.base.scope), original)

    def test_half_scope_and_new_protection_are_enforced_on_server(self):
        proposal = self.propose('后半段重新配图')
        count = len(self.base.current['shots'])
        self.assertEqual([int(s['shot_no']) for s in proposal['response']['shots']], list(range(count // 2 + 1, count + 1)))
        target = proposal['response']['affected_shot_ids'][0]
        self.f.base.workspace.set_assistant_locks(
            self.f.token, **self.base.scope, expected_project_version=self.base.current['project_version'],
            locked_shot_ids=[target], idempotency_key='protect-after-proposal',
        )
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.revision.confirm_image_batch(
                self.f.token, task_id=proposal['task']['task_id'],
                proposal_digest=proposal['response']['proposal_digest'],
            )
        self.assertEqual(caught.exception.code, 'ASSISTANT_LOCKED')
        self.assertEqual(self.f.workspace.read_progress(self.f.token, **self.base.scope)['revision'], 0)
