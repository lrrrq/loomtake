import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from mvp_v2.creative_revision import effective_brief_values
from mvp_v2.formal_advisor import FormalAdvisorDecision
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests.test_mvp_v2_formal_agent import FormalAgentFixture


class ApplicationBriefBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        advisor = SimpleNamespace(decide=lambda *_: FormalAdvisorDecision(
            intent='needs_clarification', summary='请补充三项。',
            questions=['在哪播放？', '总时长多少？', '必须出现什么？'],
            question_fields=['使用场景', '目标时长', '必须出现的内容'], candidate_action='none'))
        self.fixture = FormalAgentFixture(root, advisor=advisor)
        self.adapter = FormalBriefInputAdapter(FormalBriefWorkspaceService(self.fixture.store,
            artifact_root=root / 'data'), data_root=root / 'data')
        self.fixture.agent.script_revision = SimpleNamespace(brief_adapter=self.adapter)
        self.scope = {'workspace_id': self.fixture.workspace_id, 'project_id': self.fixture.project['project_id']}
        self.adapter.import_uploads(self.fixture.token, **self.scope, expected_project_version=0,
            uploads=[UploadedDocument('匿名原始需求.txt', BytesIO('脚本类型：年会\n品牌：匿名品牌'.encode()), True)])
        self.session = self.fixture.session['agent_session_id']
        response = self.fixture.agent.analyze_brief(self.fixture.token, self.session, content='还需要什么？')
        self.answers = [{**{key: value[key] for key in ('question_id', 'analysis_task_id')}, 'content': content}
            for value, content in zip(response['response']['question_bindings'], ['会场大屏', '60s', '新品亮相'])]
        self.project_version = self.fixture.projects.get_project(self.fixture.token, self.scope['project_id'])['project_version']

    def send(self, **changes):
        return self.fixture.agent.answer_brief_question(self.fixture.token, self.session,
            **{'answers': self.answers, 'content': '', 'expected_project_version': self.project_version,
               'idempotency_key': 'anonymous-batch-001', **changes})

    def current(self):
        return self.adapter.service.read_current_formal(self.fixture.token, **self.scope)

    def test_all_three_answers_save_once_and_replay_after_refresh(self):
        saved = self.send()
        current = self.current()
        values = effective_brief_values(current)
        self.assertEqual(values['目标时长'], '60s')
        self.assertIn('会场大屏', values['使用场景'])
        self.assertIn('新品亮相', values['必须出现的内容'])
        self.assertEqual(values['品牌或客户'], '匿名品牌')
        self.assertEqual(len(current['brief']['source_manifest']['documents']), 2)
        self.assertEqual(len(saved['answered_question_ids']), 3)
        project = self.fixture.projects.get_project(self.fixture.token, self.scope['project_id'])
        self.assertEqual(project['project_version'], self.project_version + 1)
        replay = self.send()
        self.assertEqual(replay['brief_key'], saved['brief_key'])
        self.assertEqual(self.current(), current)

    def test_invalid_later_answer_does_not_save_first_answer(self):
        before = self.current()
        answers = [dict(item) for item in self.answers]
        answers[1]['question_id'] = 'missing-question'
        with self.assertRaises(WorkspaceServiceError):
            self.send(answers=answers)
        self.assertEqual(self.current(), before)
        self.assertEqual(self.fixture.projects.get_project(self.fixture.token, self.scope['project_id'])['project_version'],
                         self.project_version)

    def test_reused_send_key_rejects_changed_answer_without_overwrite(self):
        self.send()
        before = self.current()
        answers = [dict(item) for item in self.answers]
        answers[1]['content'] = '90s'
        with self.assertRaises(WorkspaceServiceError) as error:
            self.send(answers=answers)
        self.assertEqual(error.exception.code, 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(self.current(), before)

    def test_free_composer_content_preserves_original_sources(self):
        self.send(answers=[], content='目标时长：60s\n风格调性：温暖自然')
        values = effective_brief_values(self.current())
        self.assertEqual(values['目标时长'], '60s')
        self.assertEqual(values['品牌或客户'], '匿名品牌')


if __name__ == '__main__':
    unittest.main()
