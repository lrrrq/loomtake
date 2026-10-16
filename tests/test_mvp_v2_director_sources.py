"""Source readback preserves provenance and the same content for both models."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from mvp_v2.director_conversation import _REFERENCE_CONTEXT_CHARS, project_context
from mvp_v2.formal_script_assistant import _editor_context
from mvp_v2.formal_advisor import FormalAdvisorService, FormalAdvisorPolicy
from mvp_v2.workspace_store import WorkspaceServiceError


class DirectorSourceTests(unittest.TestCase):
    def setUp(self):
        self.agent = SimpleNamespace(services=SimpleNamespace(store=None,
            tasks=SimpleNamespace(get_agent_session=lambda *args: {'project_id': 'anonymous'}),
            projects=SimpleNamespace(get_project=lambda *args: {'project_id': 'anonymous'})))
        self.agent.project_knowledge = Mock()
        self.agent.project_knowledge.list_project_conventions.return_value = {
            'conventions': [], 'inactive_statements': [], 'knowledge_version': 0}
        history_patch = patch('mvp_v2.director_conversation.history_context', return_value={})
        self.addCleanup(history_patch.stop)
        history_patch.start()

    def document(self, texts, identity='doc1'):
        return {'source_document_id': identity, 'filename': identity + '.docx',
            'content_sha256': 'a' * 64, 'brief_version_id': 'brief-original', 'status': 'success',
            'locations': [{'text': text, 'evidence': {'source_document_id': identity,
                'artifact_ref': 'source:' + identity + ':' + str(index),
                'content_sha256': 'b' * 64, 'location': {'paragraph': index}}}
                for index, text in enumerate(texts, 1)]}

    def read(self, documents, *, instruction='', model=True, **kwargs):
        base = kwargs.pop('base', {'brief': {'facts': [{'value': '已确认正式参数'}]}})
        return project_context(self.agent, 'normal', 'session',
            base={**base, 'reference_documents': documents}, instruction=instruction,
            for_model=model, **kwargs)

    def assert_budget(self, context):
        self.assertLessEqual(len(json.dumps(context['reference_documents'], ensure_ascii=False,
            sort_keys=True)), _REFERENCE_CONTEXT_CHARS)

    def test_selected_catalogue_citations_are_valid_but_removed_sources_are_not(self):
        self.agent.project_knowledge.list_project_conventions.return_value['conventions'] = [
            {'status': 'active', 'statement': '保留真实脚步声', 'rule_id': 'rule-1'}]
        context = self.read([self.document(['已确认现场说明'])],
            base={'brief': {'facts': []}, 'evidence_ids': ['brief:confirmed']})
        self.assertIn('project:conventions', context['evidence_ids'])
        self.assertIn('document:doc1', context['evidence_ids'])
        payload = {'intent': 'discussion', 'summary': '引用已选择的资料和约定',
            'candidate_action': 'none', 'evidence_ids': ['project:conventions', 'document:doc1']}
        service = FormalAdvisorService(FormalAdvisorPolicy(
            invoke=lambda *args: json.dumps(payload), provider='test', model='test'))
        service.decide('根据资料讨论', context)
        removed = self.read([self.document(['已确认现场说明'])],
            selection_explicit=True, source_selection=[])
        self.assertNotIn('document:doc1', removed['evidence_ids'])
        with self.assertRaises(WorkspaceServiceError) as error:
            service.decide('根据资料讨论', removed)
        self.assertEqual(error.exception.code, 'FORMAL_ADVISOR_EVIDENCE_INVALID')

    def test_tail_restriction_and_over_eighty_locations_reach_both_models_unchanged(self):
        tail = '禁止把内部样片报价用于对外传播。'
        document = self.document(['前文。' * 3000 + tail] + ['独立原文位置'] * 90)
        original = deepcopy(document)
        context = self.read([document])
        editor = _editor_context({**context, 'mode': 'creative_workbench'})
        self.assertEqual(editor['reference_documents'], context['reference_documents'])
        self.assertIn(tail, editor['reference_documents'][0]['locations'][0]['text'])
        self.assertEqual(len(editor['reference_documents'][0]['locations']), 91)
        self.assertEqual(editor['reference_documents'][0]['omitted_text_characters'], 0)
        self.assertEqual(document, original)
        self.assert_budget(context)

    def test_oversized_single_location_reads_relevant_tail_with_exact_original_range(self):
        tail = '霁蓝拍摄只允许用暖色背景，禁止冷光。'
        document = self.document(['首段背景。' * 12000 + tail])
        context = self.read([document], instruction='霁蓝拍摄对背景与冷光有什么要求？')
        readback = context['reference_documents'][0]
        self.assertTrue(readback['locations'])
        self.assertTrue(any(tail in location['text'] for location in readback['locations']))
        self.assertEqual(readback['context_status'], 'partial')
        self.assertGreater(readback['omitted_text_characters'], 0)
        for location in readback['locations']:
            excerpt = location['source_excerpt']
            self.assertEqual(location['text'], document['locations'][0]['text'][
                excerpt['char_start']:excerpt['char_end']])
            self.assertEqual(excerpt['total_characters'], len(document['locations'][0]['text']))
            self.assertEqual(location['evidence'], document['locations'][0]['evidence'])
        self.assertTrue(context['source_context_warnings'])
        self.assert_budget(context)

    def test_explicit_late_paragraph_is_read_before_early_irrelevant_locations(self):
        document = self.document(['早期背景。' * 500] * 90 + ['最后要求：人物不能拿起样品。'])
        context = self.read([document], instruction='请说明第91段的限制，并据此改写。')
        locations = context['reference_documents'][0]['locations']
        self.assertIn(91, [item['evidence']['location']['paragraph'] for item in locations])
        self.assertIn('人物不能拿起样品', json.dumps(locations, ensure_ascii=False))
        self.assertEqual(_editor_context(context)['reference_documents'], context['reference_documents'])
        self.assert_budget(context)

    def test_budget_counts_escaped_text_metadata_and_each_selected_document(self):
        documents = [self.document(['带引号的资料："\\\n' * 3000], f'doc{index}') for index in range(8)]
        context = self.read(documents)
        self.assert_budget(context)
        self.assertEqual(len(context['reference_documents']), 8)
        self.assertTrue(context['source_context_warnings'])
        visible = self.read(documents, model=False)
        self.assertTrue(any(item['status'] == 'partial' and '部分原文' in item['summary']
            for item in visible['context_sources'] if item['kind'] == 'document'))

    def test_removal_preserves_confirmed_facts_and_current_selected_image_identity(self):
        facts = [{'field': 'fact' + str(index), 'value': '正式依据' + '甲' * 8100} for index in range(82)]
        images = [{'media_id': 'saved-media', 'content_sha256': 'c' * 64,
            'source_file': 'anonymous.xlsx', 'source_sheet': 'Sheet1', 'source_row': 9}]
        base = {'brief': {'facts': facts, 'brief_version_id': 'confirmed'},
            'creative': {'blocks': [{'number': 1, 'text': '当前已保存段落'}], 'image_references': images},
            'project_images': {'message': '可选素材信息'}, 'images': {'message': '额外配图状态'}}
        context = self.read([self.document(['临时资料原文'])], base=base,
            source_selection=[], selection_explicit=True)
        self.assertEqual(context['reference_documents'], [])
        self.assertEqual(context['brief'], base['brief'])
        self.assertEqual(context['creative']['image_references'], images)
        self.assertNotIn('project_images', context)
        self.assertNotIn('images', context)
        self.assertIn('document:doc1', context['excluded_source_ids'])
        source = next(item for item in context['sources'] if item['id'] == 'project:creative')
        self.assertTrue(source['required'])
        self.assertIn('已选配图', source['title'])
        self.assertIn('已保存的配图选择', source['summary'])
        self.assertIn('不证明客户现场事实', source['summary'])
        editor = _editor_context({**context, 'latest_project_brief': base['brief'],
            'frozen_basis': {'brief': base['brief']}})
        self.assertEqual(editor['brief'], base['brief'])
        self.assertEqual(editor['latest_project_brief'], base['brief'])
        self.assertEqual(editor['frozen_basis']['brief'], base['brief'])

    def test_each_source_uses_an_existing_project_route(self):
        visible = self.read([self.document(['项目资料原文'])], model=False, base={
            'brief': {'facts': [{'value': '已确认需求'}]},
            'creative': {'blocks': [{'number': 1}]}, 'script': {'shots': [{'shot_no': 1}]},
            'project_images': {'message': '项目素材'}, 'images': {'message': '配图状态'},
            'review': {'message': '审核状态'}})
        actual = {item['id']: item['href'] for item in visible['context_sources']}
        self.assertEqual(actual, {
            'project:brief': '/projects/anonymous/brief',
            'project:creative': '/projects/anonymous/script',
            'project:script': '/projects/anonymous/script',
            'project:project_images': '/projects/anonymous/materials',
            'project:images': '/projects/anonymous/script?panel=images',
            'project:review': '/projects/anonymous/review',
            'document:doc1': '/projects/anonymous/brief'})


if __name__ == '__main__':
    unittest.main()
