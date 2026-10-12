import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from mvp_v2.models import SourceShot
from mvp_v2.scoring import assess_template_candidates, match_template_slots, source_candidate_id
from mvp_v2.image_retrieval import assess_image_candidates, search_images_for_slot
from mvp_v2.planning_core import compile_plan_artifact
from mvp_v2.scoring import assess_planning_requirements
from mvp_v2.template_rules import builtin_template_rules


class SemanticMatchingTests(unittest.TestCase):
    def test_failed_image_assessment_keeps_redacted_server_diagnostics_only(self):
        slot = {'画面描述': '宾客步入会场', 'current_visual_authority': True}
        rows = [{'image_id': 'entry', '原图路径': '/anonymous/entry.png', 'content_sha256': 'a' * 64,
                 'asset_role': 'source_reference', 'source_semantic_text': '交代场地与入场人群',
                 'source_links': [{'source_file': 'music.xlsx', 'source_sheet': '创意库', 'source_row': 12}]}]
        analysis = [{'image_id': 'entry', 'caption': '宾客步入会场', 'tags': ['人群'],
                     'brand_names': [], 'negative_flags': [], 'quality_flags': []}]
        policy = SimpleNamespace(writer=lambda *_: '{"api_key":"private-value","matches":',
                                 writer_provider='fixture', writer_model='fixture')
        with tempfile.TemporaryDirectory() as root:
            result = assess_image_candidates(slot, {}, rows, analysis, policy, root)
            self.assertEqual(result['status'], 'failed')
            saved = json.loads(next(Path(root).glob('*.json')).read_text())
            self.assertEqual(saved['_diagnostics']['error_type'], 'JSONDecodeError')
            self.assertNotIn('private-value', json.dumps(saved))
            public = search_images_for_slot(slot, {}, rows, analysis, semantic_assessment=result)
            self.assertNotIn('_diagnostics', public['semantic_assessment'])
            self.assertNotIn('raw_response', public['semantic_assessment'])
            self.assertEqual(public['semantic_assessment']['status'], 'failed')

    def product_plan(self):
        from tests.test_mvp_v2_round3_creative_requirements_binding import Round3CreativeRequirementsBindingTests
        fixture = Round3CreativeRequirementsBindingTests()
        fixture.setUp()
        _, brief, *_ = fixture.compile()
        constraints = {'template_rule_contract': {'payload': builtin_template_rules('launch'), 'source': 'fixture'}}
        plan = compile_plan_artifact(brief, fixture.slots, source_view='ai-spec', constraints=constraints)
        return fixture.slots, brief, constraints, plan

    @staticmethod
    def product_assessment(prompt, *_):
        data = json.loads(prompt.split('\nINPUT:', 1)[1])
        # Anonymous brand/identity and captions share the appearance shot;
        # the distinct cooking actions retain distinct event identities.
        targets = [1, 2, 2, 2, 2, 2, 3, 4, 5, 6, 7, 7, 7]
        assert len(data['requirements']) == len(targets)
        return json.dumps({'bindings': [
            {'requirement_id': item['requirement_id'], 'event_id': data['events'][target - 1]['event_id'],
             'relation': 'attribute' if target in (1, 2, 7) else 'same_action',
             'reason': '本镜保留该主体属性或对应的明确动作。'}
            for item, target in zip(data['requirements'], targets)],
            'structures': [{'event_id': event['event_id'], 'rule_id': data['structural_rules'][arc - 1]['rule_id'],
                'reason': '以产品使用过程承担既有叙事功能。'}
                for event, arc in zip(data['events'], [1, 1, 2, 2, 3, 3, 4])]}, ensure_ascii=False)

    def test_product_attributes_share_seven_existing_events_under_original_structure(self):
        slots, brief, constraints, before = self.product_plan()
        self.assertGreater(len(before.shots), 7, 'fixture must reproduce the original over-expansion')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'planning-semantics.json'
            result = assess_planning_requirements(brief, before,
                SimpleNamespace(writer=self.product_assessment, writer_model='fixture'), path)
            self.assertEqual(result['status'], 'ready', result)
            plan = compile_plan_artifact(brief, slots, source_view='ai-spec',
                constraints={**constraints, 'planning_semantics': result})
            self.assertEqual(plan.audit['status'], 'pass', plan.audit)
            self.assertEqual([shot['primary_event'] for shot in plan.shots], [event['title'] for event in before.events])
            self.assertEqual(len(plan.shots), 7)
            self.assertEqual([shot['duration_seconds'] for shot in plan.shots], [8] * 7)
            self.assertEqual([shot['structure'] for shot in plan.shots],
                ['开场造势'] * 2 + ['人气与现场互动'] * 2 + ['核心宣讲与仪式'] * 2 + ['品牌落版'])
            self.assertEqual([item['text'] for item in plan.requirements], [item['text'] for item in before.requirements])
            coverage = {item['requirement_id']: item for item in plan.coverage}
            for item in result['bindings']:
                self.assertTrue(coverage[item['requirement_id']]['shot_nos'])
            self.assertEqual(json.loads(path.read_text())['input_sha256'], result['input_sha256'])

    def test_semantic_failure_drift_or_fake_identity_never_discards_requirements(self):
        from copy import deepcopy
        slots, brief, constraints, before = self.product_plan()
        valid = assess_planning_requirements(brief, before,
            SimpleNamespace(writer=self.product_assessment, writer_model='fixture'))
        invalid = deepcopy(valid)
        invalid['bindings'][0]['event_id'] = 'invented'
        stale = deepcopy(valid)
        stale['input_sha256'] = '0' * 64
        incomplete = deepcopy(valid)
        incomplete['bindings'].pop()
        failed = assess_planning_requirements(brief, before,
            SimpleNamespace(writer=lambda *_: '{}', writer_model='fixture'))
        self.assertEqual(failed['status'], 'failed')
        self.assertTrue(failed['input'])
        for assessment in (invalid, stale, incomplete, failed):
            with self.subTest(status=assessment['status']):
                plan = compile_plan_artifact(brief, slots, source_view='ai-spec',
                    constraints={**constraints, 'planning_semantics': assessment})
                self.assertEqual(len(plan.shots), len(before.shots))
                self.assertEqual([row['text'] for row in plan.requirements], [row['text'] for row in before.requirements])

    def test_canonical_generator_freezes_and_consumes_semantics_from_real_workbook(self):
        from mvp_v2.loop_runner import _generate
        from tests.test_mvp_v2_formal_generation_service import FormalGenerationServiceTests
        fixture = FormalGenerationServiceTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, brief, constraints, _ = self.product_plan()
        calls = []
        def writer(prompt, *args):
            calls.append(prompt)
            return self.product_assessment(prompt, *args)
        path = fixture.root / 'semantics.json'
        result = _generate([brief], fixture.source_root, constraints, source_view='ai-spec',
            semantic_policy=SimpleNamespace(writer=writer, writer_model='fixture'), semantic_evidence_path=path)
        rows, _, _, _, _, plan = result
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(rows), 7)
        self.assertEqual(plan['planning_semantics']['status'], 'ready')
        self.assertEqual(plan['planning_semantics']['provider_calls'], 1)
        self.assertEqual(plan['planning_semantics'], json.loads(path.with_name('planning_semantics.json').read_text()))
        self.assertEqual([shot['primary_event'] for shot in plan['shot_plans']],
            ['厨房与产品入场', '外观及型号', '备米放入内锅', '控制面板', '成饭开盖', '盛饭上桌', '产品片尾'])

    def test_semantic_binding_cannot_override_explicit_speech_structure_or_negation(self):
        from copy import deepcopy
        from mvp_v2.planning_core import planning_semantic_input, validate_planning_semantics
        _, brief, _, plan = self.product_plan()
        result = assess_planning_requirements(brief, plan,
            SimpleNamespace(writer=self.product_assessment, writer_model='fixture'))
        events = deepcopy(plan.events)
        events[0]['title'] = '负责人核心发言'
        inputs = planning_semantic_input(brief, events, plan.requirements, plan.template_rule_contract)
        with self.assertRaisesRegex(ValueError, 'concrete event'):
            validate_planning_semantics(inputs, events, result)
        inputs = planning_semantic_input(brief, plan.events, plan.requirements, plan.template_rule_contract)
        inputs['requirements'][0]['text'] = '不要展示内部结构'
        with self.assertRaisesRegex(ValueError, 'polarity'):
            validate_planning_semantics(inputs, plan.events, result)

    def test_cross_category_audit_requires_frozen_library_and_never_changes_template_category(self):
        from mvp_v2.audit import audit_generation
        from mvp_v2.models import TemplateDecision
        slot = SourceShot('年会', 'annual.xlsx', '模版', 7, slot_index=1, structure='开场')
        variant = SourceShot('音乐节', 'music.xlsx', '创意库', 12, structure='开场',
            brand_neutral=True, brand_neutral_evidence='人工确认:无品牌标识',
            source_package_id='music', source_package_version=6, source_category_id='music_festival', source_asset_id='row-12')
        decision = TemplateDecision(slot=slot, variant=variant, mode='ADAPT', score=88, match_reason='通用意图相符')
        binding = {'library_bindings':[{'category_id':'music_festival','package_id':'music','package_version':6,
            'package_sha256':'a'*64,'resolved_ref':'music@6'}]}
        def audit(evidence, template=slot):
            return audit_generation({'脚本类型':'年会','视频时长':'5s'}, [template], [decision],
                [{'结构':'开场','镜号':'1','画面描述':'场地入口','时长(秒)':5}],
                [{'slot_index':1,'模板镜号':'1','采用来源文件':'music.xlsx','采用来源 sheet':'创意库','采用来源行':12}],
                asset_package_evidence=evidence)['blocking_items']
        self.assertIn('cross_category_source_forbidden', audit(None))
        self.assertNotIn('cross_category_source_forbidden', audit(binding))
        self.assertIn('cross_category_source_forbidden', audit(binding, replace(slot,script_type='音乐节')))
        binding['library_bindings'][0]['package_version'] = 7
        self.assertIn('cross_category_source_forbidden', audit(binding))

    def test_cross_category_intent_retains_real_source_and_safety(self):
        slot = SourceShot('年会', 'annual.xlsx', '模版', 7, slot_index=1, structure='开场',
            generic_desc='交代活动规模、场地与主题', sheet_overview='约60秒；若室外活动则保留天气氛围。')
        variant = SourceShot('音乐节', 'music.xlsx', '创意库', 12, structure='演出前',
            generic_desc='交代活动规模、场地与主题', visual_desc='无品牌的会场入口人群',
            brand_neutral=True, brand_neutral_evidence='人工确认:无品牌标识',
            source_package_id='music', source_package_version=6, source_category_id='music_festival', source_asset_id='real-row-12')
        unsafe = replace(variant, source_row=13, source_asset_id='forbidden-row', reuse_level='禁止复用')
        captured = []
        def writer(prompt, *_):
            captured.append(prompt)
            return json.dumps({'matches': [{'slot_index': 1, 'candidate_id': source_candidate_id(variant),
                'status': 'adapt', 'score': 88, 'hits': ['规模与场地'], 'missing': ['本项目主题标识'],
                'conflicts': [], 'reason': '保留入口人流构图，用本项目主题重新拍摄。'}]})
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'assessment.json'
            assessment = assess_template_candidates({'脚本类型': '年会'}, [slot], [variant, unsafe], {},
                SimpleNamespace(writer=writer, writer_model='fixture'), path)
            decisions = match_template_slots({'脚本类型': '年会'}, [slot], [variant, unsafe], {'semantic_matching': assessment})
            self.assertEqual(decisions[0].mode, 'ADAPT')
            self.assertEqual(decisions[0].variant.source_package_version, 6)
            self.assertEqual(decisions[0].slot.script_type, '年会')
            self.assertIn('交代活动规模、场地与主题', captured[0])
            self.assertIn('若室外活动则保留天气氛围', captured[0])
            self.assertNotIn(source_candidate_id(unsafe), captured[0])
            self.assertEqual(json.loads(path.read_text())['input_sha256'], assessment['input_sha256'])

    def test_fabricated_template_identity_fails_without_rule_auto_adoption(self):
        slot = SourceShot('年会', 'a.xlsx', '模版', 7, slot_index=1, structure='开场')
        variant = replace(slot, source_sheet='创意库', brand_neutral=True, brand_neutral_evidence='人工确认:无品牌')
        policy = SimpleNamespace(writer=lambda *_: json.dumps({'matches': [{'slot_index': 1, 'candidate_id': 'invented',
            'status': 'match', 'score': 100, 'hits': [], 'missing': [], 'conflicts': [], 'reason': '虚构'}]}), writer_model='fixture')
        result = assess_template_candidates({'脚本类型': '年会'}, [slot], [variant], {}, policy)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(match_template_slots({'脚本类型': '年会'}, [slot], [variant], {'semantic_matching': result})[0].mode, 'KEEP')

    def test_image_semantics_use_safe_real_ids_and_never_auto_adopt(self):
        slot = {'画面描述': '宾客步入会场', 'current_visual_authority': True}
        brief = {'脚本类型': '年会'}
        rows = [{'image_id': 'real-entry', 'content_sha256': 'a' * 64, '原图路径': '/anonymous/entry.png',
                 'asset_role': 'source_reference', 'source_links': [{'source_file': 'music.xlsx', 'source_sheet': '创意库', 'source_row': 12}],
                 'source_semantic_text': '交代活动规模、场地与主题'},
                {'image_id': 'forbidden', '原图路径': '/anonymous/forbidden.png'}]
        analysis = [{'image_id': 'real-entry', 'caption': '来宾进入入口大厅', 'tags': ['人群'], 'brand_names': [], 'negative_flags': [], 'quality_flags': []},
                    {'image_id': 'forbidden', 'caption': '来宾入场', 'tags': ['人群'], 'negative_flags': ['forbidden_content']}]
        calls = []
        def writer(prompt, *_):
            calls.append(prompt)
            return json.dumps({'matches': [{'image_id': 'real-entry', 'score': 92, 'hits': ['宾客入场'],
                'missing': [], 'conflicts': [], 'reason': '来宾进入与宾客步入表达相同活动目的。'}]})
        policy = SimpleNamespace(writer=writer, writer_model='fixture', writer_provider='fixture')
        with tempfile.TemporaryDirectory() as root:
            assessment = assess_image_candidates(slot, brief, rows, analysis, policy, root)
            self.assertEqual(assess_image_candidates(slot, brief, rows, analysis, policy, root), assessment)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('"image_id": "forbidden"', calls[0])
            result = search_images_for_slot(slot, brief, rows, analysis, top_k=10, semantic_assessment=assessment)
            selected = result['candidates'][0]
            self.assertEqual(selected['image_id'], 'real-entry')
            self.assertIn('表达相同', selected['match_reason'])
            self.assertFalse(selected['default_eligible'])
            self.assertTrue(selected['requires_human_review'])
            self.assertEqual(result['status'], 'review')
