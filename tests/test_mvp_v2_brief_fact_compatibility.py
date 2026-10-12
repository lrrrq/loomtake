"""Anonymous Brief fact compatibility and evidence-bound inference regressions.

Exercise the authority's pure merge functions. No database, Provider, browser,
customer document or production runtime is used.
"""
from copy import deepcopy
import unittest

from mvp_v2.formal_brief_input_adapter import (
    _build_facts, _facts_from_candidates, _merge_understood_facts, _pdf_paragraph_locations,
)
from mvp_v2.script_type_adapter import resolve_script_type_selection


class BriefFactCompatibilityTests(unittest.TestCase):
    def check_merge(self, field, values, *, conflict):
        entries = [{'value': value, 'evidence_refs': [f'artifact://anonymous-source/{index}']}
                   for index, value in enumerate(values)]
        original = deepcopy(entries)
        facts, conflicts = _facts_from_candidates({field: entries})
        self.assertEqual(bool(conflicts), conflict, (field, values, facts, conflicts))
        if not conflict:
            self.assertEqual(len(facts), 1)
        self.assertEqual({ref for fact in facts for ref in fact['evidence_refs']},
                         {ref for entry in entries for ref in entry['evidence_refs']})
        self.assertEqual(entries, original, 'Original source values must remain intact')
        return facts

    def test_usage_punctuation_and_complementary_purposes_merge(self):
        cases = [
            ['用于新品发布会播放。16:9，1920×1080。厨房与餐桌。',
             '用于新品发布会播放；16:9，1920×1080；厨房与餐桌'],
            ['用于新品发布会播放；拍摄在厨房', '同时用于视频号横屏；拍摄在相邻餐桌'],
        ]
        for values in cases:
            with self.subTest(values=values):
                facts = self.check_merge('使用场景', values, conflict=False)
                if values == cases[1]:
                    for phrase in ('新品发布会', '视频号', '厨房', '餐桌'):
                        self.assertIn(phrase, facts[0]['value'])

    def test_usage_opposing_orientation_remains_conflict(self):
        self.check_merge('使用场景', ['视频号16:9横屏', '视频号9:16竖屏'], conflict=True)

    def test_script_type_explanation_uses_existing_category_resolution(self):
        values = ['发布会', '发布会。做的是现场播放的产品介绍短片，片中是家居厨房准备晚饭。']
        resolved = [resolve_script_type_selection(value) for value in values]
        self.assertEqual(resolved[0]['category_id'], resolved[1]['category_id'])
        self.check_merge('脚本类型', values, conflict=False)
        self.check_merge('脚本类型', ['发布会', '展会'], conflict=True)

    def test_brand_identity_allows_explanation_but_not_other_or_joint_brand(self):
        self.check_merge('品牌或客户', ['匿名甲',
            '品牌匿名甲。产品是匿名甲电饭煲MODEL-01，4L、IH加热。',
            '匿名甲。依据：官方产品参数。'], conflict=False)
        for other in ('匿名乙', '匿名甲与匿名乙联合品牌', '匿名甲、匿名乙'):
            with self.subTest(other=other):
                self.check_merge('品牌或客户', ['匿名甲', other], conflict=True)

    def test_duration_total_is_not_first_seconds_and_bare_total_is_compatible(self):
        for expanded in ('整片56秒，共7镜，每镜8秒。片尾包含在56秒内。',
                         '每镜8秒，共7镜，总56秒'):
            with self.subTest(expanded=expanded):
                self.check_merge('目标时长', ['56秒', expanded], conflict=False)

    def test_duration_conflicting_totals_structure_and_internal_claims_remain_conflicts(self):
        cases = [
            ['56秒', '整片64秒，共8镜，每镜8秒'],
            ['整片56秒，共7镜，每镜8秒', '整片56秒，共8镜，每镜7秒'],
            ['整片56秒，共7镜，每镜8秒', '整片56秒，共7镜，每镜7秒'],
            ['56秒', '整片56秒，共7镜，每镜9秒'],
            ['56秒', '整片56秒，视频总时长64秒'],
        ]
        for values in cases:
            with self.subTest(values=values):
                self.check_merge('目标时长', values, conflict=True)

    def test_bare_duration_does_not_bridge_incompatible_structures(self):
        values = ['56秒', '整片56秒，共7镜，每镜8秒', '整片56秒，共8镜，每镜7秒']
        facts = self.check_merge('目标时长', values, conflict=True)
        self.assertEqual(len(facts), 2)
        by_structure = {('7镜' in fact['value'], '8镜' in fact['value']): fact
                        for fact in facts}
        self.assertEqual(set(by_structure), {(True, False), (False, True)})
        for key, source in (((True, False), 1), ((False, True), 2)):
            self.assertEqual(set(by_structure[key]['evidence_refs']),
                             {'artifact://anonymous-source/0', f'artifact://anonymous-source/{source}'})

    def test_same_brand_preserves_different_product_explanations_for_human_review(self):
        descriptions = ['品牌匿名甲。产品是MODEL-01，容量4L。',
                        '品牌匿名甲。产品是MODEL-02，容量5L。']
        facts = self.check_merge('品牌或客户', descriptions, conflict=False)
        # Brand identity may agree while product facts still require inspection.
        # Neither explanation may disappear behind the shared brand signature.
        summary = facts[0]['value']
        for description in descriptions:
            for sentence in description.rstrip('。').split('。'):
                clauses = sentence.split('，')
                offsets = [summary.index(clause) for clause in clauses]
                self.assertEqual(offsets, sorted(offsets), 'Product description order must survive')
        self.assertEqual(set(facts[0]['evidence_refs']),
                         {'artifact://anonymous-source/0', 'artifact://anonymous-source/1'})

    def test_pdf_visual_line_wrap_matches_full_text_without_absorbing_next_section(self):
        goal = '让观众认识产品外观，通过准备晚饭的过程建立日常使用印象。'
        scene = '用于新品发布会和视频号横屏，拍摄场景是明亮厨房。'
        style = '自然克制，从准备晚饭推进到上桌。'
        raw_pdf = ('视频目标：让观众认识产品外观，通过准备晚饭的过程建立日常使\n'
                   '用印象。\n使用场景：用于新品发布会和视频号横屏，拍摄\n'
                   '场景是明亮厨房。\n风格调性：自然克制，从准备晚饭推\n'
                   '进到上桌。\n产品事实与来源\n'
                   '品牌匿名甲的MODEL-01参数需要依据原始产品资料核实。')
        sources = [
            {'document_id': 'anonymous_pdf', 'status': 'succeeded', 'locations': [
                {'location': {'page': 1}, 'evidence_ref': 'artifact://anonymous-source/pdf/page/1',
                 'text': raw_pdf}]},
            {'document_id': 'anonymous_txt', 'status': 'succeeded', 'locations': [
                {'location': {'line': 1}, 'evidence_ref': 'artifact://anonymous-source/txt/line/1',
                 'text': '视频目标：' + goal}]},
        ]
        original = deepcopy(sources)
        facts, conflicts = _build_facts(sources)
        self.assertEqual(conflicts, [])
        by_field = {fact['field']: fact for fact in facts}
        self.assertEqual(by_field['视频目标']['value'].rstrip('。'), goal.rstrip('。'))
        self.assertEqual(set(by_field['视频目标']['evidence_refs']),
                         {'artifact://anonymous-source/pdf/page/1', 'artifact://anonymous-source/txt/line/1'})
        for field, value in (('使用场景', scene), ('风格调性', style)):
            self.assertEqual(by_field[field]['value'].rstrip('。'), value.rstrip('。'))
            self.assertNotIn('产品事实与来源', by_field[field]['value'])
            self.assertNotIn('MODEL-01', by_field[field]['value'])
        self.assertEqual(sources, original, 'PDF evidence raw text must not be rewritten')

    def test_three_page_pdf_recognizes_standalone_required_content_heading(self):
        pages = ['项目名称：匿名产品发布短片\n视频目标：介绍厨房产品。',
                 '使用场景：明亮厨房。\n风格调性：自然克制。',
                 '必须出现的内容\n匿名甲品牌、MODEL-01型号与4L容量。\n'
                 '产品外观、备米、合盖、盛饭和上桌都需要清晰出现。']
        sources = [{'document_id': 'anonymous_three_page_pdf', 'status': 'succeeded',
                    'locations': [{'location': {'page': index},
                                   'evidence_ref': f'artifact://anonymous-source/three-page/page/{index}',
                                   'text': text} for index, text in enumerate(pages, 1)]}]
        original = deepcopy(sources)
        facts, conflicts = _build_facts(sources)
        self.assertEqual(conflicts, [])
        required = [fact for fact in facts if fact['field'] == '必须出现的内容']
        self.assertEqual(len(required), 1)
        for phrase in ('匿名甲品牌', 'MODEL-01型号', '4L容量', '产品外观', '备米', '合盖', '盛饭', '上桌都需要清晰出现'):
            self.assertIn(phrase, required[0]['value'])
        self.assertEqual(set(required[0]['evidence_refs']),
                         {'artifact://anonymous-source/three-page/page/3'})
        self.assertNotIn('明亮厨房', required[0]['value'])
        self.assertEqual(sources, original)

    def test_spreadsheet_brand_header_cell_does_not_consume_following_table(self):
        cells = [('A1', '品牌'), ('B1', '匿名甲'), ('A2', '型号'), ('B2', 'MODEL-01'),
                 ('A3', '容量'), ('B3', '4L'), ('A4', '加热方式'), ('B4', 'IH加热')]
        sources = [{'document_id': 'anonymous_product_xlsx', 'status': 'succeeded',
                    'locations': [{'location': {'sheet': '产品参数', 'cell': cell},
                                   'evidence_ref': f'artifact://anonymous-source/xlsx/sheet/product/cell/{cell}',
                                   'text': text} for cell, text in cells]}]
        original = deepcopy(sources)
        facts, conflicts = _build_facts(sources)
        self.assertEqual(conflicts, [])
        # Unlabelled spreadsheet cells need table-aware extraction or inference;
        # a bare header is not permission to consume every later cell as brand.
        self.assertEqual([fact for fact in facts if fact['field'] == '品牌或客户'], [])
        self.assertEqual(sources, original)

    def test_unknown_pdf_heading_ends_known_section_before_unrelated_body(self):
        paragraphs = [(True, '特殊需求'),
                      (False, '采用16:9横屏，片尾完整保留“好好吃饭，认真生活”。'),
                      (True, '片尾表达'),
                      (False, '以下是另一段创意讨论，不应成为特殊需求字段。')]
        sources = [{'document_id': 'anonymous_paragraph_pdf', 'status': 'succeeded',
                    'locations': [{'location': {'page': 3, 'paragraph': index, **({'heading': True} if heading else {})},
                                   'evidence_ref': f'artifact://anonymous-source/pdf/page/3/paragraph/{index}',
                                   'text': text} for index, (heading, text) in enumerate(paragraphs, 1)]}]
        original = deepcopy(sources)
        facts, conflicts = _build_facts(sources)
        self.assertEqual(conflicts, [])
        special = [fact for fact in facts if fact['field'] == '特殊需求']
        self.assertEqual(len(special), 1)
        self.assertIn('“好好吃饭，认真生活”', special[0]['value'])
        self.assertNotIn('片尾表达', special[0]['value'])
        self.assertNotIn('另一段创意讨论', special[0]['value'])
        self.assertEqual(set(special[0]['evidence_refs']),
                         {'artifact://anonymous-source/pdf/page/3/paragraph/2'})
        self.assertEqual(sources, original)

    def test_pdf_visitor_rejoins_fragments_and_quoted_wrap_before_fact_parsing(self):
        fragments = [
            ('视频目标：让观众认识', 700, 11), ('MODEL-01', 700, 11),
            ('外观，通过准备晚饭建立日常使', 700, 11), ('用印象。', 685, 11),
            ('特殊需求', 655, 12),
            ('16:9横屏，片尾完整保留“好好吃饭，认真生', 630, 11),
            ('活”，不增加旁白或虚构参数。', 615, 11),
            ('片尾表达', 580, 12),
            ('这里是另一段尚未确认的创意讨论，不属于特殊需求。', 555, 11),
        ]
        raw_text = ('视频目标：让观众认识MODEL-01外观，通过准备晚饭建立日常使\n用印象。\n'
                    '特殊需求\n16:9横屏，片尾完整保留“好好吃饭，认真生\n活”，不增加旁白或虚构参数。\n'
                    '片尾表达\n这里是另一段尚未确认的创意讨论，不属于特殊需求。')

        class AnonymousPdfPage:
            def extract_text(self, *, visitor_text):
                for text, y, font_size in fragments:
                    visitor_text(text, [1, 0, 0, 1, 0, 0],
                                 [1, 0, 0, 1, 40, y], {'/BaseFont': '/Anonymous'}, font_size)
                return raw_text

        returned_raw, locations = _pdf_paragraph_locations(AnonymousPdfPage(), 'anonymous_pdf', 3)
        self.assertEqual(returned_raw, raw_text)
        self.assertTrue(all(item['location']['page'] == 3 and 'paragraph' in item['location']
                            for item in locations))
        self.assertEqual([item['text'] for item in locations if item['location'].get('heading')],
                         ['特殊需求', '片尾表达'])
        facts, conflicts = _build_facts([{'document_id': 'anonymous_pdf', 'status': 'succeeded',
                                         'locations': locations}])
        self.assertEqual(conflicts, [])
        by_field = {item['field']: item for item in facts}
        self.assertEqual(by_field['视频目标']['value'].rstrip('。'),
                         '让观众认识MODEL-01外观，通过准备晚饭建立日常使用印象')
        special = by_field['特殊需求']['value']
        self.assertIn('“好好吃饭，认真生活”', special)
        self.assertNotIn('认真生 活', special)
        self.assertNotIn('片尾表达', special)
        self.assertNotIn('创意讨论', special)
        body = next(item for item in locations if '认真生活' in item['text'])
        self.assertEqual(by_field['特殊需求']['evidence_refs'], [body['evidence_ref']])

    @staticmethod
    def direct_sources(field, direct, independent_text=None):
        sources = [{'document_id': 'anonymous_direct', 'status': 'succeeded', 'locations': [
            {'evidence_ref': 'artifact://anonymous-source/direct', 'text': f'{field}：{direct}'}]}]
        if independent_text is not None:
            sources.append({'document_id': 'anonymous_independent', 'status': 'succeeded', 'locations': [
                {'evidence_ref': 'artifact://anonymous-source/independent', 'text': independent_text}]})
        facts, conflicts = _build_facts(sources)
        return {'extractions': sources, 'facts': facts, 'conflicts': conflicts}

    def test_same_direct_source_does_not_reinject_brand_or_goal_interpretation(self):
        for field, direct, inferred in (
            ('品牌或客户', '匿名甲', '品牌匿名甲；产品为MODEL-01电饭煲'),
            ('视频目标', '让观众看清产品外观和准备晚饭的过程', '强调MODEL-01的容量与IH技术优势'),
        ):
            with self.subTest(field=field):
                extracted = self.direct_sources(field, direct)
                before = deepcopy(extracted)
                _merge_understood_facts(extracted, [{'field': field, 'value': inferred,
                    'evidence_refs': ['artifact://anonymous-source/direct']}])
                self.assertEqual(extracted['facts'], before['facts'])
                self.assertEqual(extracted['conflicts'], [])
                self.assertEqual(extracted['extractions'], before['extractions'])

    def test_independent_new_source_goal_still_requires_business_resolution(self):
        extracted = self.direct_sources('视频目标', '让观众看清产品外观和准备晚饭的过程',
                                        '本轮强调MODEL-01容量与IH技术优势。')
        before = deepcopy(extracted['extractions'])
        _merge_understood_facts(extracted, [{'field': '视频目标',
            'value': '强调MODEL-01的容量与IH技术优势',
            'evidence_refs': ['artifact://anonymous-source/independent']}])
        self.assertEqual([item['field'] for item in extracted['conflicts']], ['视频目标'])
        goals = [fact for fact in extracted['facts'] if fact['field'] == '视频目标']
        self.assertEqual(len(goals), 2)
        self.assertEqual({ref for fact in goals for ref in fact['evidence_refs']},
                         {'artifact://anonymous-source/direct', 'artifact://anonymous-source/independent'})
        self.assertEqual(extracted['extractions'], before)

    def test_goal_text_is_not_generically_semantically_merged(self):
        self.check_merge('视频目标', ['认识产品的外观和准备晚饭的过程',
                                  '认识产品的外观、容量和IH技术优势'], conflict=True)
        self.check_merge('视频目标', ['片尾完整保留“好好吃饭，认真生活”',
                                  '片尾完整保留“好好吃饭；认真生活”'], conflict=True)
