from __future__ import annotations

import json
import unittest

from mvp_v2.formal_script_assistant import (
    FormalScriptAssistantEditorPolicy,
    analyze_creative_blocks,
    analyze_formal_script_instruction,
)


class ResolvedEditorScopeTests(unittest.TestCase):
    def setUp(self):
        self.shots = [{
            'shot_id': f'anonymous-block-{number:02}', 'shot_no': str(number),
            'structure': '匿名段落', 'camera_type': '固定机位', 'shot_size': '中景',
            'camera_move': '固定', 'visual': '匿名会场中的活动画面。',
            'sound': '现场环境声', 'voiceover': '', 'subtitle': '', 'notes': '',
        } for number in range(1, 26)]
        self.selected = [self.shots[index]['shot_id'] for index in (1, 5)]
        self.ceiling = {identity: ['visual', 'sound'] for identity in self.selected}
        self.pairs = {(identity, field) for identity in self.selected for field in ('visual', 'sound')}

    def analyze(self, stage, operations, *, protected=(), resolved=True, instruction=None):
        unit = '段' if stage == 'creative' else '镜'
        constraints = {
            'preserve': [f'第2{unit}与第6{unit}各自时长不变',
                         f'两{unit}除画面描述与声音外的其他全部字段不变'],
            'prohibit': [f'不修改两{unit}以外的其他字段'],
        }
        instruction = instruction or (
            f'只修改已选第2{unit}和第6{unit}的画面描述与声音，再按修改后的画面推荐参考图。'
            f'第2{unit}突出入口通道，声音改为清晰脚步；第6{unit}突出自然互动合影，声音补充快门声。'
            f'保持总时长60秒、25{unit}结构、两{unit}各自时长及其他全部字段不变。'
            '\n保留与禁止要求：' + json.dumps(constraints, ensure_ascii=False)
        )
        prompts = []

        def invoke(prompt, _model, _tokens):
            prompts.append(json.loads(prompt.split('\nDATA:', 1)[1]))
            return json.dumps({'schema_version': 'mplus-formal-script-assistant-editor/v1',
                               'operations': operations}, ensure_ascii=False)

        policy = FormalScriptAssistantEditorPolicy(invoke=invoke, provider='anonymous', model='anonymous')
        ceiling = self.ceiling if resolved else None
        if stage == 'creative':
            result = analyze_creative_blocks(instruction=instruction,
                blocks=[{'shot_id': row['shot_id'], 'shot_no': row['shot_no'], 'fields': {
                    '段落': row['structure'], '画面描述': row['visual'], '声音': row['sound'], '时长': '2',
                }} for row in self.shots], selected_block_ids=self.selected,
                protected_block_ids=list(protected), editor_policy=policy, allowed_fields_by_shot=ceiling)
        else:
            result = analyze_formal_script_instruction(instruction=instruction, shots=self.shots,
                assistant_locked_shot_ids=list(protected), selected_shot_ids=self.selected,
                editor_policy=policy, allowed_fields_by_shot=ceiling)
        return result, prompts

    def legal_operations(self):
        return [{'shot_id': identity, 'field': field,
                 'value': '入口通道中的匿名活动画面。' if field == 'visual' else '清晰脚步与快门声。'}
                for identity in self.selected for field in ('visual', 'sound')]

    def test_resolved_creative_pairs_survive_collective_and_exclusion_preservation(self):
        self.assert_legal_pairs('creative')

    def test_resolved_formal_pairs_survive_collective_and_exclusion_preservation(self):
        self.assert_legal_pairs('formal')

    def assert_legal_pairs(self, stage):
        result, prompts = self.analyze(stage, self.legal_operations())
        self.assertEqual(result['type'], 'rewrite_operations', result)
        self.assertEqual(len(prompts), 1)
        self.assertEqual({(item['shot_id'], item['field']) for item in prompts[0]['targets']}, self.pairs)
        self.assertEqual({(item['shot_id'], item['field']) for item in result['operations']}, self.pairs)

    def test_resolved_scope_still_rejects_an_extra_field_or_unselected_identity(self):
        for stage in ('creative', 'formal'):
            for illegal in ({'shot_id': self.selected[0], 'field': 'structure', 'value': '越界段落'},
                            {'shot_id': self.shots[3]['shot_id'], 'field': 'visual', 'value': '越界画面'}):
                with self.subTest(stage=stage, illegal=illegal):
                    result, prompts = self.analyze(stage, [*self.legal_operations(), illegal])
                    self.assertEqual(result['analysis_code'], 'editor_scope_violation', result)
                    self.assertNotIn('operations', result)
                    self.assertEqual({(item['shot_id'], item['field']) for item in prompts[0]['targets']}, self.pairs)

    def test_resolved_scope_does_not_allow_modifying_a_protected_identity(self):
        for stage in ('creative', 'formal'):
            with self.subTest(stage=stage):
                result, prompts = self.analyze(stage, self.legal_operations(), protected=[self.selected[0]])
                self.assertEqual(result['type'], 'needs_clarification', result)
                self.assertNotIn('operations', result)
                self.assertIn(result['analysis_code'], {'editor_scope_violation', 'protected_target_selected'})
                self.assertTrue(all(target['shot_id'] != self.selected[0]
                                    for prompt in prompts for target in prompt['targets']))

    def test_legacy_entry_still_applies_explicit_preservation_without_resolved_scope(self):
        for stage in ('creative', 'formal'):
            with self.subTest(stage=stage):
                unit = '段' if stage == 'creative' else '镜'
                result, prompts = self.analyze(stage,
                    [{'shot_id': self.selected[0], 'field': 'sound', 'value': '清晰脚步声。'}], resolved=False,
                    instruction=f'修改第2{unit}声音，第6{unit}声音保持不变。')
                self.assertEqual(result['type'], 'rewrite_operations', result)
                self.assertEqual([(item['shot_id'], item['field']) for item in prompts[0]['targets']],
                                 [(self.selected[0], 'sound')])


if __name__ == '__main__':
    unittest.main()
