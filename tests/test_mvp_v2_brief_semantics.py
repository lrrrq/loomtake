import json
import unittest

from mvp_v2.creative_revision import effective_brief_facts, effective_brief_values
from mvp_v2.formal_brief_input_adapter import _understand_brief, FormalBriefUnderstandingPolicy, _fact_semantics
from mvp_v2.formal_script_assistant import _editor_context
from mvp_v2.planning_core import _semantic_match_score, _special_classification


class BriefSemanticsTests(unittest.TestCase):
    def test_only_selected_supported_facts_reach_generation(self):
        raw = {'brief': {'facts': [
            {'fact_id': 'old', 'field_path': '品牌或客户', 'value_json': '"旧品牌"'},
            {'fact_id': 'new', 'field_path': '品牌或客户', 'value_json': '"当前品牌"'},
            {'fact_id': 'sales', 'field_path': '特殊需求', 'value_json': '"销量100万"',
             'semantic_metadata': {'support_status': 'needs_review'}},
        ], 'conflicts': [{'field_path': '品牌或客户', 'fact_ids': ['old', 'new'],
                          'resolved_fact_id': 'new', 'resolution_status': 'resolved'}]}}
        self.assertEqual([item['fact_id'] for item in effective_brief_facts(raw)], ['new'])
        self.assertEqual(effective_brief_values(raw), {'品牌或客户': '当前品牌'})
        raw['brief']['conflicts'][0]['resolution_status'] = 'unresolved'
        self.assertEqual(effective_brief_facts(raw), [])

    def test_genuine_quote_does_not_support_invented_sales_but_keeps_other_fact(self):
        extracted = {'facts': [], 'conflicts': [], 'extractions': [{'status': 'succeeded', 'document_id': 'doc',
            'locations': [{'text': '希望观众了解产品的易用性。', 'location': {'paragraph': 1}, 'evidence_ref': 'source:1'}]}]}
        def model(*args):
            return json.dumps({'facts': [
                {'field': '视频目标', 'value': '销量达到100万台', 'evidence': [{'key': 'E0001', 'quote': '希望观众了解产品的易用性。'}]},
                {'field': '必须出现的内容', 'value': '产品的易用性', 'evidence': [{'key': 'E0001', 'quote': '产品的易用性'}]},
            ]}, ensure_ascii=False)
        _understand_brief(extracted, FormalBriefUnderstandingPolicy(invoke=model, provider='anonymous', model='anonymous'))
        facts = {item['field']: item for item in extracted['facts']}
        self.assertEqual(facts['视频目标']['semantic_metadata']['support_status'], 'needs_review')
        self.assertEqual(facts['必须出现的内容']['semantic_metadata']['support_status'], 'supported')
        self.assertEqual(facts['必须出现的内容']['semantic_metadata']['quotes'][0]['quote'], '产品的易用性')
        self.assertFalse(extracted['conflicts'])

    def test_global_audio_light_and_post_text_are_not_extra_events(self):
        for text in ('全片声音保持轻松自然', '全片音乐与声音保持轻松自然', '采用自然光', '片尾字幕后期叠加', '不需要旁白'):
            self.assertNotEqual(_special_classification(text), 'mandatory_content')
            metadata = _fact_semantics('特殊需求', text, [], origin='literal_extraction')
            self.assertNotEqual(metadata['clauses'][0]['classification'], 'mandatory_content')
        self.assertEqual(_semantic_match_score('展示产品内部结构', '不要展示产品内部结构'), 0)
        self.assertGreater(_semantic_match_score('嘉宾入场', '来宾步入会场'), 0)

    def test_editor_keeps_complete_frozen_conditions_and_exceptions(self):
        statement = '若夜场则保留屏幕光。' * 1000 + '但产品色不得偏移，未经确认不得增加人物。'
        context = _editor_context({'effective_knowledge': {'rules': [{'scope': 'project', 'statement': statement}]},
            'template_semantics': {'sheets': [{'overview_cells': [{'text': '约60秒，不是固定60秒。'}]}]}})
        self.assertEqual(context['effective_knowledge']['rules'][0]['statement'], statement)
        self.assertIn('约60秒', str(context['template_semantics']))


if __name__ == '__main__':
    unittest.main()
