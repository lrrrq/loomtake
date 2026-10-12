"""RV01: real draft-core regressions; anonymous historical multi-shot fixture."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_script_workspace as fixture


class ScriptSaveRecoveryTests(unittest.TestCase):
    setUp = fixture.FormalScriptWorkspaceTests.setUp

    def current(self):
        return self.workspace.read_current(self.generated.token,
            workspace_id=self.generated.workspace_id, project_id=self.generated.project['project_id'])

    def change(self, field, value, key):
        model = self.current()
        return self.workspace.apply_table_change(self.generated.token,
            workspace_id=self.generated.workspace_id, project_id=self.generated.project['project_id'],
            expected_project_version=model['project_version'], expected_revision=model['revision'],
            shot_no='1', shot_id=model['shots'][0]['shot_id'], field=field, value=value, idempotency_key=key)

    def test_optional_clear_replay_undo_redo_preserves_other_shots_and_parent(self):
        initial = self.current()
        for field in ('voiceover', 'subtitle', 'notes'):
            with self.subTest(field=field):
                filled = self.change(field, '匿名可选文字', field + '-fill')
                args = dict(workspace_id=self.generated.workspace_id,
                    project_id=self.generated.project['project_id'],
                    expected_project_version=filled['project_version'], expected_revision=filled['revision'],
                    shot_no='1', shot_id=filled['shots'][0]['shot_id'], field=field,
                    value='  ', idempotency_key=field + '-clear')
                cleared = self.workspace.apply_table_change(self.generated.token, **args)
                self.assertEqual(cleared['shots'][0][field], '')
                self.assertEqual(self.workspace.apply_table_change(self.generated.token, **args)['revision'], cleared['revision'])
                history = dict(workspace_id=self.generated.workspace_id,
                    project_id=self.generated.project['project_id'], expected_project_version=cleared['project_version'])
                undone = self.workspace.undo(self.generated.token, **history,
                    expected_revision=cleared['revision'], idempotency_key=field + '-undo')
                self.assertEqual(undone['shots'][0][field], '匿名可选文字')
                redone = self.workspace.redo(self.generated.token, **history,
                    expected_revision=undone['revision'], idempotency_key=field + '-redo')
                self.assertEqual(redone['shots'][0][field], '')
                self.assertEqual(redone['shots'][1:], initial['shots'][1:])
                self.assertEqual(redone['base_version_id'], initial['base_version_id'])
        self.assertEqual(len(self.projects.list_project_versions(self.generated.token,
            self.generated.project['project_id'])), 1)

    def test_invalid_values_leave_revision_unchanged_and_corrected_input_saves(self):
        self.change('subtitle', '已有草稿', 'historical-draft')
        for field, value in [('visual', ''), ('sound', ' '), ('subtitle', True),
                             ('subtitle', 'x' * 4001)] + [('duration_seconds', v) for v in (0, True, 1.5, 3601, '2')]:
            with self.subTest(field=field, value=str(value)[:20]):
                before = self.current()
                with self.assertRaises(WorkspaceServiceError):
                    self.change(field, value, 'bad-' + field)
                self.assertEqual(self.current()['revision'], before['revision'])
        self.assertEqual(self.change('duration_seconds', 8, 'correct-duration')['shots'][0]['duration_seconds'], 8)
        self.assertEqual(self.change('subtitle', '继续编辑', 'continue-subtitle')['shots'][0]['subtitle'], '继续编辑')

    def test_missing_optional_field_is_not_added(self):
        original = self.workspace._subject_shots
        def without_notes(context):
            shots = deepcopy(original(context))
            shots[0]['fields'].pop('备注', None)
            return shots
        with patch.object(self.workspace, '_subject_shots', side_effect=without_notes):
            with self.assertRaisesRegex(WorkspaceServiceError, '补丁目标镜头或字段不存在'):
                self.change('notes', '', 'missing-notes')
        self.assertEqual(self.current()['revision'], 0)
