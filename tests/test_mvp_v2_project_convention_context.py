"""Current project convention lifecycle over immutable model evidence."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from mvp_v2.workspace_store import WorkspaceServiceError
from tests.test_mvp_v2_knowledge_service import KnowledgeServiceFixture


class ProjectConventionContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fixture = KnowledgeServiceFixture(Path(temporary.name))
        self.knowledge = self.fixture.knowledge
        self.auth = self.fixture.editor_auth
        self.project_id = self.fixture.launch['project_id']
        self.scope = {'workspace_id': self.fixture.workspace_id, 'project_id': self.project_id}

    def version(self):
        return self.knowledge.get_state(self.auth, **self.scope)['knowledge_version']

    def add(self, statement):
        return self.knowledge.add_project_convention(self.auth, **self.scope, statement=statement,
            evidence_refs=['artifact://anonymous/message'], expected_knowledge_version=self.version(),
            confirmed=True)

    def stop(self, convention, status='disabled'):
        return self.knowledge.set_project_convention_status(self.auth, **self.scope,
            convention_id=convention['convention_id'], status=status,
            reason='当前项目明确停止使用', expected_knowledge_version=self.version())

    def test_only_retired_project_rule_id_is_removed_without_mutating_frozen_facts(self):
        statement = '结尾保留人物回望'
        retired = self.add(statement)
        company = self.fixture.approve(self.fixture.propose(scope='company', statement=statement))
        category = self.fixture.approve(self.fixture.propose(scope='category', statement=statement))
        effective = self.knowledge.list_effective_rules(self.auth, self.project_id)
        legacy = {'rule_id': 'legacy-approved-project-rule', 'scope': 'project', 'statement': statement}
        effective['rules'].append(legacy)
        effective['snapshot_sha256'] = 'a' * 64
        context = {'brief': {'已确认内容': statement, 'effective_knowledge': deepcopy(effective)},
            'effective_knowledge': effective, 'frozen_basis': {
                'brief': {'已确认内容': statement}, 'effective_knowledge': deepcopy(effective)},
            'creative_standard': {'必须内容': statement}, 'source_summary': {'text': statement}}
        original = deepcopy(context)
        self.stop(retired)
        projected = self.knowledge.project_convention_context(self.auth, self.project_id, context=context)
        expected = {company['rule_id'], category['rule_id'], legacy['rule_id']}
        self.assertEqual({item['rule_id'] for item in projected['effective_knowledge']['rules']}, expected)
        self.assertEqual({item['rule_id'] for item in projected['frozen_basis']['effective_knowledge']['rules']}, expected)
        self.assertEqual(projected['effective_knowledge']['snapshot_sha256'], 'a' * 64)
        self.assertEqual(projected['brief'], original['brief'])
        self.assertEqual(projected['frozen_basis']['brief'], original['frozen_basis']['brief'])
        self.assertEqual(projected['creative_standard'], original['creative_standard'])
        self.assertEqual(projected['source_summary'], original['source_summary'])
        self.assertEqual(context, original)
        self.assertEqual(projected['project_conventions'], [])
        self.assertEqual(projected['retired_conventions'][0]['rule_id'], retired['rule_id'])

    def test_correction_and_later_same_text_convention_remain_distinct_from_retired_identity(self):
        first = self.add('一直保留现场人声')
        frozen_rule = {'rule_id': first['rule_id'], 'scope': 'project', 'statement': first['statement']}
        corrected = self.knowledge.correct_project_convention(self.auth, **self.scope,
            convention_id=first['convention_id'], statement='开头保留人声，结尾仅用环境声',
            evidence_refs=['artifact://anonymous/correction'], expected_knowledge_version=self.version(),
            reason='明确更正约定范围', confirmed=True)
        later = self.add(first['statement'])
        context = {'effective_knowledge': [frozen_rule],
            'frozen_basis': {'effective_knowledge': [deepcopy(frozen_rule)]}}
        projected = self.knowledge.project_convention_context(self.auth, self.project_id, context=context)
        self.assertEqual(projected['effective_knowledge'], [])
        self.assertEqual(projected['frozen_basis']['effective_knowledge'], [])
        self.assertEqual({item['rule_id'] for item in projected['project_conventions']},
            {corrected['rule_id'], later['rule_id']})
        self.assertEqual(projected['retired_conventions'][0]['rule_id'], first['rule_id'])
        self.assertEqual(context['effective_knowledge'], [frozen_rule])

    def test_deleted_legacy_rule_without_scope_retires_but_explicit_company_scope_is_preserved(self):
        convention = self.add('只在本项目使用铜铃过渡')
        self.stop(convention, status='deleted')
        old_rule = {'rule_id': convention['rule_id'], 'statement': convention['statement']}
        company = {**old_rule, 'scope': 'company'}
        context = {'effective_knowledge': {'rules': [old_rule, company]}}
        projected = self.knowledge.project_convention_context(self.auth, self.project_id, context=context)
        self.assertEqual(projected['effective_knowledge']['rules'], [company])
        self.assertEqual(projected['retired_conventions'][0]['status'], 'deleted')

    def test_new_context_reads_current_conventions_and_enforces_project_permission(self):
        active = self.add('新会话仍保留自然现场声')
        projected = self.knowledge.project_convention_context(self.auth, self.project_id, context={})
        self.assertEqual(projected['project_conventions'][0]['rule_id'], active['rule_id'])
        self.assertEqual(projected['knowledge_version'], self.version())
        self.assertNotIn('effective_knowledge', projected)
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.knowledge.project_convention_context(self.auth,
                self.fixture.exhibition['project_id'], context={})
        self.assertEqual(caught.exception.code, 'PERMISSION_DENIED')


if __name__ == '__main__':
    unittest.main()
