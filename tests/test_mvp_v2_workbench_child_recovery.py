"""A persisted child must retain its parent even before the parent receipt links it."""
import unittest

from fastapi.testclient import TestClient

from mvp_v2 import creative_workbench
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.local_ui import create_app
from mvp_v2.workbench_task_recovery import parent_for_child, sync_workbench
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_creative_workbench as workbench_fixture


class _InterruptedWorker(BaseException):
    """Emulate process termination, which ordinary provider error handling cannot catch."""


class WorkbenchChildRecoveryTests(unittest.TestCase):
    def test_formal_child_cannot_be_reselected_into_a_standalone_proposal(self):
        f = workbench_fixture.FormalWorkbenchTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        proposal = f.propose('formal-child-reselection-boundary')
        data = creative_workbench._load(f.agent, f.token, proposal['task']['task_id'])[1]
        current = f.workspace.read_current(f.token, **f.scope)
        before, database = f.business(), f.database()
        with self.assertRaises(WorkspaceServiceError) as rejected:
            f.revision.reselect_proposal(f.token, project_id=f.scope['project_id'],
                task_id=data['text_task_id'], expected_project_version=current['project_version'],
                expected_revision=current['revision'], retained_shot_ids=[f.targets[0]],
                idempotency_key='formal-child-standalone-reselection')
        self.assertEqual(rejected.exception.code, 'WORKBENCH_CONFIRMATION_REQUIRED')
        self.assertEqual(f.database(), database)
        self.assertEqual(f.business(), before)
        self.assertEqual(parent_for_child(f.agent.services.tasks, f.token, data['text_task_id']),
                         proposal['task']['task_id'])

    def test_ready_text_child_after_interruption_still_requires_parent_confirmation(self):
        f = workbench_fixture.CreativeWorkbenchTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        before = f.business()
        started = creative_workbench.start(f.agent, f.token, f.session['agent_session_id'],
            content='修改第1、2段画面和声音，再给这两段重新推荐配图，保留其他内容。',
            context=f.context(), operation_key='interrupted-child-before-parent-receipt')
        parent_id = started['task']['task_id']
        original = f.revision.propose_creative_draft_change

        def interrupted_after_child(*args, **kwargs):
            original(*args, **kwargs)
            raise _InterruptedWorker()

        f.revision.propose_creative_draft_change = interrupted_after_child
        try:
            with self.assertRaises(_InterruptedWorker):
                creative_workbench.execute(f.agent, f.token, parent_id)
        finally:
            f.revision.propose_creative_draft_change = original

        # The old parent event deliberately has no child pointer. The child must
        # remain attributable using its own persisted authority record.
        parent_data = creative_workbench._load(f.agent, f.token, parent_id)[1]
        self.assertIsNone(parent_data['text_task_id'])
        recovered = sync_workbench(f.new_agent(), f.token, parent_id)
        self.assertEqual(recovered['task']['status'], 'failed')
        self.assertEqual(recovered['response']['error_code'], 'WORKBENCH_INTERRUPTED')

        client = TestClient(create_app(agent_api=f.agent, formal_generation_service=f.generation,
            formal_image_service=f.image, formal_brief_adapter=f.generation.brief_input_adapter,
            formal_brief_upload_factory=uploaded_document))
        self.addCleanup(client.close)
        headers = {'X-MPlus-Agent-Auth': f.token, 'X-MPlus-UI-Token': client.app.state.session_token}
        params = {'workspace_id': f.scope['workspace_id']}
        project_url = f"/api/formal/projects/{f.scope['project_id']}"
        draft = client.get(project_url + '/creative-draft', headers=headers, params=params)
        self.assertEqual(draft.status_code, 200, draft.text)
        draft = draft.json()
        child_id = draft['task']['task_id']
        self.assertEqual(draft['task']['status'], 'waiting_user')
        self.assertEqual(parent_for_child(f.agent.services.tasks, f.token, child_id), parent_id)
        self.assertEqual(draft['proposal']['workbench_parent_task_id'], parent_id)
        self.assertEqual(draft['task']['workbench_parent_task_id'], parent_id)

        session = client.get(f"/api/agent/sessions/{f.session['agent_session_id']}", headers=headers).json()
        self.assertEqual(next(task for task in session['tasks'] if task['task_id'] == child_id)
                         ['workbench_parent_task_id'], parent_id)
        self.assertTrue(all(item.get('workbench_parent_task_id') == parent_id
            for item in session['timeline'] if item.get('task_id') == child_id))
        bypass = client.post(project_url + '/creative-draft/revisions/confirm', headers=headers,
            params=params, json={'task_id': child_id, 'proposal_digest': draft['proposal']['digest'],
                                 'retained_block_ids': ['block_1']})
        self.assertEqual(bypass.status_code, 409, bypass.text)
        self.assertEqual(bypass.json()['detail']['code'], 'WORKBENCH_CONFIRMATION_REQUIRED')
        self.assertEqual(f.business(), before)


if __name__ == '__main__':
    unittest.main()
