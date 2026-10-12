"""Live provider waits and interrupted-task recovery with real authority services."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
import unittest

from mvp_v2 import creative_workbench
from mvp_v2.workbench_task_recovery import sync_workbench
from tests import test_mvp_v2_creative_workbench as workbench_fixture


class WorkbenchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = workbench_fixture.CreativeWorkbenchTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.instruction = '修改选中两段的画面和声音，并按新画面寻找配图；保留其他段落。'

    def start(self, key):
        return creative_workbench.start(self.f.agent, self.f.token,
            self.f.session['agent_session_id'], content=self.instruction,
            context=self.f.context(), operation_key=key)

    def assert_live_session_read(self, provider_name):
        entered, release = Event(), Event()
        owner = self.f.library if provider_name == 'retrieval' else self.f.provider
        attribute = 'candidates' if provider_name == 'retrieval' else provider_name
        provider = getattr(owner, attribute)

        def blocked_provider(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('test did not release the model response')
            return provider(*args, **kwargs)

        setattr(owner, attribute, blocked_provider)
        self.f.provider.install(self.f.agent)
        started = self.start('live-workbench-' + provider_name)
        task_id = started['task']['task_id']
        before = self.f.business()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(creative_workbench.execute, self.f.agent, self.f.token, task_id)
            try:
                self.assertTrue(entered.wait(10), 'the real execution never reached the model')
                tasks = self.f.agent.services.tasks.list_workspace_tasks(self.f.token,
                    self.f.scope['workspace_id'], project_id=self.f.scope['project_id'])
                live = {task['task_id']: task['status'] for task in tasks if task['status'] == 'executing'}
                self.assertIn(task_id, live)
                if provider_name == 'editor':
                    self.assertTrue(any(task['task_type'] == 'formal_creative_draft'
                        and task['status'] == 'executing' for task in tasks))
                if provider_name == 'retrieval':
                    self.assertGreaterEqual(len(live), 2, 'image retrieval must have a live child task')
                database = self.f.database()
                self.assertEqual(sync_workbench(self.f.agent, self.f.token, task_id)['task']['status'], 'executing')
                session = self.f.agent.get_session(self.f.token, self.f.session['agent_session_id'])
                current = {task['task_id']: task['status'] for task in session['tasks']}
                self.assertEqual({identity: current[identity] for identity in live}, live)
                self.assertEqual(self.f.database(), database, 'polling a live task must not write recovery events')
                self.assertEqual(self.f.business(), before)
            finally:
                release.set()
            result = future.result(timeout=10)
        self.assertEqual(result['task']['status'], 'waiting_user', result['response'])
        self.assertEqual(result['response']['type'], 'workbench_proposal')
        self.assertEqual(len(self.f.provider.advisor_inputs), 1)
        self.assertEqual(len(self.f.provider.editor_inputs), 1)
        self.assertEqual(self.f.business(), before)

    def test_advisor_in_flight_session_read_preserves_live_parent(self):
        self.assert_live_session_read('advisor')

    def test_editor_in_flight_session_read_preserves_live_parent_and_child(self):
        self.assert_live_session_read('editor')

    def test_image_retrieval_in_flight_session_read_preserves_live_parent_and_child(self):
        self.assert_live_session_read('retrieval')

    def test_restart_without_live_lock_marks_interrupted_once_and_keeps_request(self):
        started = self.start('interrupted-workbench')
        task_id = started['task']['task_id']
        task, data, response = creative_workbench._load(self.f.agent, self.f.token, task_id)
        data['decision'] = {'summary': '已理解要求，尚未采用'}
        preserved = deepcopy(data)
        creative_workbench._record(self.f.agent, self.f.token, task, data, response, 'executing')
        before = self.f.business()
        restarted_agent = self.f.new_agent()
        result = sync_workbench(restarted_agent, self.f.token, task_id)
        self.assertEqual(result['task']['status'], 'failed')
        self.assertEqual(result['response']['type'], 'workbench_failed')
        self.assertEqual(result['response']['error_code'], 'WORKBENCH_INTERRUPTED')
        self.assertIn('尚未采用', result['response']['summary'])
        self.assertEqual(result['response']['choices'],
            [{'label': '重新预览这条要求', 'instruction': self.instruction}])
        self.assertEqual(creative_workbench._load(restarted_agent, self.f.token, task_id)[1], preserved)
        database = self.f.database()
        replay = sync_workbench(restarted_agent, self.f.token, task_id)
        self.assertEqual(replay['response'], result['response'])
        self.assertEqual(replay['task']['status'], result['task']['status'])
        self.assertEqual(replay['task']['task_version'], result['task']['task_version'])
        self.assertEqual(self.f.database(), database)
        self.assertEqual(self.f.business(), before)
        self.assertFalse(self.f.provider.advisor_inputs)
        self.assertFalse(self.f.provider.editor_inputs)

    def test_completed_adoption_rereads_receipt_without_recovery_or_model_calls(self):
        proposal = self.f.propose('completed-workbench-recovery')
        applied = self.f.confirm(proposal)
        before, database = self.f.business(), self.f.database()
        calls = (len(self.f.provider.advisor_inputs), len(self.f.provider.editor_inputs))
        result = sync_workbench(self.f.new_agent(), self.f.token, proposal['task']['task_id'])
        self.assertEqual(result['response'], applied['response'])
        self.assertEqual(result['task']['status'], 'completed')
        self.assertEqual(result['task']['task_version'], applied['task']['task_version'])
        self.assertEqual(self.f.database(), database)
        self.assertEqual(self.f.business(), before)
        self.assertEqual((len(self.f.provider.advisor_inputs), len(self.f.provider.editor_inputs)), calls)


if __name__ == '__main__':
    unittest.main()
