import tempfile
import unittest
from pathlib import Path
from tests.test_mvp_v2_ui_governance import GovernanceUiFixture
from mvp_v2.knowledge_service import KnowledgeService
from mvp_v2.workspace_store import WorkspaceServiceError

class GovernanceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.f = GovernanceUiFixture(Path(self.temp.name))
    def tearDown(self):
        self.f.client.close()
        self.temp.cleanup()

    def test_only_owned_currently_available_evidence_can_be_prepared_and_executed(self):
        path = f'/api/agent/governance/knowledge/evidence?agent_session_id={self.f.session}'
        response = self.f.client.get(path, headers=self.f.headers)
        self.assertEqual(response.status_code, 200, response.text)
        option = response.json()['items'][0]
        self.assertNotIn('artifact://', str(response.json()))
        args = dict(agent_session_id=self.f.session, statement='匿名项目规则', scope='project', idempotency_key='evidence-check-01', evidence_ids=[option['evidence_id']])
        prepared = self.f.post('/api/agent/governance/knowledge/candidates/prepare', args)
        self.assertEqual(prepared.status_code, 200, prepared.text)
        bad = self.f.post('/api/agent/governance/knowledge/candidates/prepare', {**args, 'evidence_ids':['brief:another-project']})
        self.assertEqual(bad.status_code, 409, bad.text)
        for source in self.f.briefs.root.rglob('*.json'):
            source.unlink()
        result = self.f.post('/api/agent/governance/knowledge/candidates/execute', dict(agent_session_id=self.f.session, parameters=prepared.json()['parameters'], confirmation_token=prepared.json()['confirmation_token']))
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(KnowledgeService(self.f.store).list_candidates(self.f.auth, self.f.workspace_id), [])

    def test_manual_approval_must_match_candidate_project_even_in_same_workspace(self):
        k = KnowledgeService(self.f.store)
        state = k.get_state(self.f.auth, self.f.workspace_id, project_id=self.f.project['project_id'])
        candidate = k.create_candidate(self.f.auth, workspace_id=self.f.workspace_id, project_id=self.f.project['project_id'], scope='project', target_id=self.f.project['project_id'], statement='匿名候选', evidence_refs=['artifact://anonymous/evidence'], expected_knowledge_version=state['knowledge_version'])
        ws = self.f.projects.get_workspace(self.f.auth, self.f.workspace_id)
        other = self.f.projects.create_project(self.f.auth, workspace_id=self.f.workspace_id, name='另一个项目', category_id='launch', expected_workspace_version=ws['workspace_version'])
        result = self.f.post('/api/manual/knowledge/approvals/prepare', dict(project_id=other['project_id'], candidate_id=candidate['candidate_id'], review_evidence='核对完成', idempotency_key='wrong-project-approval'), headers=self.f.manual_headers)
        self.assertEqual(result.status_code, 403, result.text)
        with self.assertRaisesRegex(WorkspaceServiceError, 'PERMISSION_DENIED'):
            k.approve_candidate(self.f.auth, workspace_id=self.f.workspace_id, project_id=other['project_id'], candidate_id=candidate['candidate_id'], level='project', review_evidence='核对完成', expected_knowledge_version=candidate['knowledge_version'])
