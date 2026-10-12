import json
import unittest

from mvp_v2.formal_script_assistant import _validate_editor_output


class DurationPreviewTests(unittest.TestCase):
    def test_complete_timing_allocation_keeps_only_changed_operations_and_reasons(self):
        result = _validate_editor_output(json.dumps({
            'schema_version': 'mplus-formal-script-assistant-editor/v1',
            'operations': [
                {'shot_id': 'a', 'field': 'duration_text', 'value': '3'},
                {'shot_id': 'b', 'field': 'duration_text', 'value': '5'},
            ],
            'shot_reasons': [
                {'shot_id': 'a', 'reason': '保持开场节奏。'},
                {'shot_id': 'b', 'reason': '延长产品亮相。'},
            ],
        }), text='只调整时长', shots={'a': {'duration_text': '3'}, 'b': {'duration_text': '4'}},
            target_pairs=[('a', 'duration_text'), ('b', 'duration_text')], allowed_values={},
            clearable_fields=set(), editable_fields=frozenset({'duration_text'}))
        operations, reasons, error = result
        self.assertIsNone(error)
        self.assertEqual([item['shot_id'] for item in operations], ['b'])
        self.assertEqual(reasons, [{'shot_id': 'b', 'reason': '延长产品亮相。'}])


if __name__ == '__main__':
    unittest.main()
