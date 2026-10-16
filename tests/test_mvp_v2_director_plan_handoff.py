"""A saved edit is not a completed plan; callbacks use frozen generation receipts."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_image_workspace import FormalImageWorkspaceService, FormalImageWorkspaceServices
from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests.test_mvp_v2_library_browse import library_confirmation_agent
from tests import test_mvp_v2_creative_workbench as workbench_fixture
from tests import test_mvp_v2_formal_script_workspace as workspace_fixture


class DirectorPlanHandoffTests(unittest.TestCase):
    def setUp(self):
        self.base = workspace_fixture.FormalScriptWorkspaceTests('runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.generation, self.workspace = self.base.generation, self.base.workspace
        self.token = self.base.generated.token
        self.scope = {'workspace_id': self.base.generated.workspace_id,
                      'project_id': self.base.generated.project['project_id']}
        self.tasks = TaskService(self.base.store)
        self.images = FormalImageWorkspaceService(FormalImageWorkspaceServices(
            store=self.base.store, projects=self.base.projects, delivery=self.base.delivery,
            artifacts=self.base.resolver, images=self.generation.services.images),
            runs_root=self.base.generated.root / 'runs', source_root=self.base.generated.source_root)
        self.adapter = FormalBriefInputAdapter(self.generation.services.briefs,
                                              data_root=self.base.generated.root)
        self.revision = FormalRevisionService(projects=self.base.projects, tasks=self.tasks,
            workspace=self.workspace, image_workspace=self.images, brief_adapter=self.adapter,
            writes_enabled=True)
        self.generation.brief_input_adapter = self.adapter
        self.provider = workbench_fixture._WorkbenchProvider(mixed=False, stage='formal')
        self.provider.actions = [
            {'kind': 'replan', 'target_scope': 'all', 'target_numbers': [], 'fields': [],
             'instruction': '先核对规划并生成正式版本'},
            {'kind': 'revise_text', 'target_scope': 'all', 'target_numbers': [], 'fields': ['sound'],
             'instruction': '规划确认后再改声音，其他字段保持', 'depends_on': [0]},
        ]
        self.agent = self.new_agent()
        self.session = self.tasks.create_agent_session(self.token, project_id=self.scope['project_id'])

    def new_agent(self):
        agent = library_confirmation_agent(root=self.base.generated.root, store=self.base.store,
            projects=self.base.projects, service=None)
        agent.script_revision, agent.generation = self.revision, self.generation
        self.provider.install(agent)
        return agent

    def current(self):
        return self.workspace.read_current(self.token, **self.scope)

    def handoff(self):
        current = self.current()
        progress = self.images.read_progress(self.token, **self.scope)
        started = self.agent.start_workbench(self.token, self.session['agent_session_id'],
            content='先核对规划生成正式版本，再修改声音；确认前先不改稿。',
            context={'stage': 'formal', 'scope': 'all', 'selected_target_ids': [],
                     'current_target_id': '', 'expected_project_version': current['project_version'],
                     'expected_revision': current['revision'], 'expected_progress_revision': progress['revision']},
            operation_key='director-plan-handoff')
        result = self.agent.execute_workbench(self.token, started['task']['task_id'])
        self.assertEqual(result['response']['type'], 'workbench_handoff', result['response'])
        return result['task']['task_id']

    def save_edit(self):
        current = self.current()
        row = current['shots'][0]
        self.workspace.apply_table_change(self.token, **self.scope,
            expected_project_version=current['project_version'], expected_revision=current['revision'],
            shot_id=row['shot_id'], shot_no=row['shot_no'], field='sound', value='用户普通保存的现场声',
            idempotency_key='director-plan-unrelated-edit')
        current = self.current()
        self.workspace.save_version(self.token, **self.scope,
            expected_project_version=current['project_version'], expected_revision=current['revision'],
            reason='保存普通声音修改，不确认规划', idempotency_key='director-plan-unrelated-save')

    def generate_plan(self):
        current = self.current()
        plan = self.generation.prepare_plan(self.token, **self.scope,
            expected_project_version=current['project_version'], generation_mode='text_only_draft')
        self.base.generated.expected_shot_count = plan['shot_count']
        project = self.base.projects.get_project(self.token, self.scope['project_id'])
        return self.generation.confirm_plan_and_generate(self.token, **self.scope,
            plan_id=plan['plan_id'], expected_project_version=project['project_version'])

    def test_ordinary_save_cannot_supply_a_plan_receipt_or_resume_dependency(self):
        task_id = self.handoff()
        self.save_edit()
        before = deepcopy(self.current())
        with self.assertRaises(WorkspaceServiceError):
            self.agent.record_workbench_handoff(self.token, task_id,
                project_id=self.scope['project_id'], kind='plan',
                result={'plan_id': 'unrelated-plan', 'version_id': before['base_version_id']})
        fresh = self.new_agent()
        resumed = fresh.resume_workbench(self.token, task_id)
        self.assertEqual(resumed['task']['task_id'], task_id)
        self.assertFalse(resumed['response'].get('handoff_receipts'))
        self.assertFalse(resumed['response']['handoff'].get('completed'))
        self.assertEqual(self.current(), before)
        self.assertEqual(len(self.provider.advisor_inputs), 1)
        self.assertEqual(self.provider.editor_inputs, [])

    def test_completed_plan_binds_actual_version_and_resumes_after_callback_loss(self):
        task_id = self.handoff()
        generated = self.generate_plan()
        fresh = self.new_agent()
        continued = fresh.resume_workbench(self.token, task_id)
        self.assertNotEqual(continued['task']['task_id'], task_id)
        receipt = fresh.read_workbench(self.token, task_id)['response']['handoff_receipts']['0']
        self.assertEqual(receipt['plan_id'], generated['plan_id'])
        self.assertEqual(receipt['current_version_id'], generated['version_id'])
        self.assertTrue(receipt['plan_sha256'])
        replay = self.new_agent().resume_workbench(self.token, task_id)
        self.assertEqual(replay['task']['task_id'], continued['task']['task_id'])
        proposal = fresh.execute_workbench(self.token, continued['task']['task_id'])
        self.assertEqual(proposal['response']['type'], 'workbench_proposal', proposal['response'])
        before = deepcopy(self.current()['shots'])
        chosen = proposal['response']['groups'][0]['group_id']
        fresh.confirm_workbench(self.token, continued['task']['task_id'],
            proposal_digest=proposal['response']['proposal_digest'], retained_group_ids=[chosen])
        self.assertNotEqual(self.current()['shots'], before)
        reread = self.new_agent().read_workbench(self.token, continued['task']['task_id'])
        self.assertIn(chosen, reread['response']['applied_group_ids'])

    def test_callback_rejects_wrong_plan_and_version_then_accepts_canonical_result(self):
        task_id = self.handoff()
        generated = self.generate_plan()
        for wrong in ({**generated, 'plan_id': 'unrelated-plan'},
                      {**generated, 'version_id': self.base.parent_version_id}):
            with self.subTest(wrong=wrong['plan_id']):
                with self.assertRaises(WorkspaceServiceError):
                    self.agent.record_workbench_handoff(self.token, task_id,
                        project_id=self.scope['project_id'], kind='plan', result=wrong)
        accepted = self.agent.record_workbench_handoff(self.token, task_id,
            project_id=self.scope['project_id'], kind='plan', result=generated)
        self.assertEqual(accepted['receipts']['0']['plan_id'], generated['plan_id'])

    def test_confirmed_recovery_receipt_resumes_without_changing_failed_plan_task(self):
        from mvp_v2.formal_admin_service import FormalAdminService
        from mvp_v2.formal_generation_service import FormalGenerationBlocked
        task_id = self.handoff()
        current = self.current()
        plan = self.generation.prepare_plan(self.token, **self.scope,
            expected_project_version=current['project_version'], generation_mode='text_only_draft')
        self.base.generated.expected_shot_count = plan['shot_count']
        project = self.base.projects.get_project(self.token, self.scope['project_id'])
        with patch('mvp_v2.formal_generation_service.run_generation_once',
                   side_effect=RuntimeError('anonymous interrupted generation')), self.assertRaises(FormalGenerationBlocked):
            self.generation.confirm_plan_and_generate(self.token, **self.scope,
                plan_id=plan['plan_id'], expected_project_version=project['project_version'])
        failed = self.tasks.get_task(self.token, plan['plan_id'])
        operations = FormalAdminService(self.base.store)
        operations.build_recovery_executor(
            {'formal_generation_plan': self.generation.recover_confirmed_generation},
            authenticated_stages=frozenset({'formal_generation_plan'}))
        proposed = operations.propose_stage_retry(self.token, **self.scope,
            requested_stage_id='formal_generation_plan', reason='恢复匿名规划生成',
            expected_project_version=project['project_version'], idempotency_key='director-recovery')
        result = operations.confirm_stage_retry(self.token, workspace_id=self.scope['workspace_id'],
            proposal_id=proposed['proposal_id'], proposal_digest=proposed['proposal_digest'])
        self.assertEqual(result['execution']['terminal_status'], 'succeeded')
        provider_calls = list(self.base.calls)
        continued = self.new_agent().resume_workbench(self.token, task_id)
        self.assertNotEqual(continued['task']['task_id'], task_id)
        receipt = self.agent.read_workbench(self.token, task_id)['response']['handoff_receipts']['0']
        self.assertEqual(receipt['plan_id'], plan['plan_id'])
        self.assertEqual(receipt['recovery_attempt_id'], result['new_attempt_id'])
        self.assertEqual(self.tasks.get_task(self.token, plan['plan_id']), failed)
        self.assertEqual(self.base.calls, provider_calls)



if __name__ == '__main__':
    unittest.main()
