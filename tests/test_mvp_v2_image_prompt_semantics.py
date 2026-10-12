import json
from dataclasses import replace
from unittest.mock import patch

from mvp_v2.creative_generation import repository
from mvp_v2.workspace_store import WorkspaceServiceError
from tests.test_mvp_v2_image_creation import ImageCreationTests


class ImagePromptSemanticsTests(ImageCreationTests):
    def setUp(self):
        super().setUp()
        self.prompts = []
        def writer(prompt, model, tokens):
            self.prompts.append(prompt)
            return json.dumps({'subject': '桌面的匿名白色产品', 'moment': '静置', 'scene': '窗边',
                'composition': '右侧主体，左侧留白', 'lighting': '自然光',
                'prompt': '窗边桌面上静置白色产品，右侧构图，左侧留白，自然光。',
                'postproduction_text': ['清新每一天'], 'conversion_notes': ['选择片尾产品静置瞬间；标语由后期叠加。']}, ensure_ascii=False)
        self.service.provider_policy = replace(self.service.provider_policy, writer=writer)
        current = self.current()
        self.target = {'target_kind': 'creative_block', 'target_id': current['blocks'][1]['block_id']}
        repo = repository(self.service, self.token, **self.scope)
        state = repo.read()
        with repo.transaction(expected_project_version=state['project_version'], expected_revision=state['revision']):
            repo.body['draft']['shots'][1]['fields']['画面描述'] = '产品静置窗边桌面。后期字幕：清新每一天。'

    def prepare(self, **kwargs):
        return self.service.prepare_image_prompt(self.token, **self.scope, **self.target, **kwargs)

    def test_prepare_edit_restart_and_visible_provider_input(self):
        before = self.current()['blocks']
        context = self.prepare(additional_instruction='给第2镜生图')
        draft = context['prepared_prompt']
        self.assertIn('后期字幕：清新每一天', self.prompts[0])
        self.assertIn('给第2镜生图', self.prompts[0])
        self.assertNotIn('给第2镜生图', draft['prompt'])
        edited = self.prepare(edited_prompt='白色产品静置窗边，暖色自然光，左侧留白。', expected_prompt_sha256=draft['sha256'])
        saved = self.service.image_creation_context(self.token, **self.scope, **self.target)
        self.assertEqual(saved['prepared_prompt'], edited['prepared_prompt'])
        self.assertEqual(self.current()['blocks'], before)
        started = self.service.start_image_creation(self.token, **self.scope, **self.target,
            expected_project_version=saved['project_version'], expected_creative_revision=saved['creative_revision'],
            expected_target_basis=saved['target_basis'], expected_prompt_sha256=saved['prepared_prompt']['sha256'],
            prompt=saved['suggested_prompt'], operation_key='prepared-prompt-image', start_background=False)
        self.execute(started)
        self.assertEqual(self.calls[0][0], saved['suggested_prompt'])
        self.assertEqual(len(self.prompts), 1)

    def test_preparation_does_not_invent_postproduction_copy(self):
        writer = self.service.provider_policy.writer
        def with_invented_copy(*args):
            value = json.loads(writer(*args))
            value['postproduction_text'].append('未提供的全新营销标语')
            return json.dumps(value, ensure_ascii=False)
        self.service.provider_policy = replace(self.service.provider_policy, writer=with_invented_copy)
        draft = self.prepare()['prepared_prompt']
        self.assertEqual(draft['postproduction_text'], ['清新每一天'])
        self.assertEqual(draft['unverified_postproduction_text'], ['未提供的全新营销标语'])
        self.assertEqual(draft['status'], 'ready')

    def test_stale_preserves_edits_until_explicit_rebase_and_rejects_mismatch(self):
        draft = self.prepare()['prepared_prompt']
        self.prepare(edited_prompt='我手动保存的产品静物画面', expected_prompt_sha256=draft['sha256'])
        repo = repository(self.service, self.token, **self.scope)
        state = repo.read()
        with repo.transaction(expected_project_version=state['project_version'], expected_revision=state['revision']):
            repo.body['draft']['shots'][1]['fields']['画面描述'] = '另一台蓝色产品在庭院'
        stale = self.prepare()
        self.assertTrue(stale['prepared_prompt']['stale'])
        self.assertEqual(stale['suggested_prompt'], '我手动保存的产品静物画面')
        with self.assertRaises(WorkspaceServiceError):
            self.service.start_image_creation(self.token, **self.scope, **self.target,
                expected_project_version=stale['project_version'], expected_creative_revision=stale['creative_revision'],
                expected_prompt_sha256=stale['prepared_prompt']['sha256'], prompt=stale['suggested_prompt'],
                operation_key='stale-prompt-request', start_background=False)
        rebased = self.prepare(use_saved_prompt=True, expected_prompt_sha256=stale['prepared_prompt']['sha256'])
        self.assertFalse(rebased['prepared_prompt']['stale'])
        self.assertEqual(rebased['suggested_prompt'], stale['suggested_prompt'])
        self.assertEqual(len(self.prompts), 1)

    def test_failed_preparation_retains_basis_and_manual_recovery(self):
        self.service.provider_policy = replace(self.service.provider_policy, writer=lambda *args: 'not-json')
        failed = self.prepare(additional_instruction='给第7镜生图')
        self.assertEqual(failed['prepared_prompt']['status'], 'failed')
        self.assertEqual(failed['suggested_prompt'], '')
        self.assertIn('source_fields', failed['prepared_prompt']['basis'])
        recovered = self.prepare(edited_prompt='片尾桌面白色产品特写，左侧留白。', expected_prompt_sha256=failed['prepared_prompt']['sha256'])
        self.assertEqual(recovered['prepared_prompt']['status'], 'ready')
        self.assertEqual(self.calls, [])

    def test_same_script_visual_context_is_frozen_without_other_shot_sound(self):
        repo = repository(self.service, self.token, **self.scope)
        state = repo.read()
        with repo.transaction(expected_project_version=state['project_version'], expected_revision=state['revision']):
            repo.body['draft']['shots'][0]['fields']['画面描述'] = '产品上盖的控制面板完整可见。'
            repo.body['draft']['shots'][0]['fields']['声音'] = '不相关的声音不作为画面依据'
        prepared = self.prepare()['prepared_prompt']
        self.assertIn('产品上盖的控制面板完整可见', self.prompts[-1])
        self.assertNotIn('不相关的声音不作为画面依据', self.prompts[-1])
        self.assertEqual(prepared['basis']['continuity_context'][0]['shot_no'], '1')
        repo = repository(self.service, self.token, **self.scope)
        state = repo.read()
        with repo.transaction(expected_project_version=state['project_version'], expected_revision=state['revision']):
            repo.body['draft']['shots'][0]['fields']['声音'] = '声音修改不改变产品外观'
        self.assertFalse(self.prepare()['prepared_prompt']['stale'])
        repo = repository(self.service, self.token, **self.scope)
        state = repo.read()
        with repo.transaction(expected_project_version=state['project_version'], expected_revision=state['revision']):
            repo.body['draft']['shots'][0]['fields']['画面描述'] = '用户修改了产品外观依据'
        stale = self.prepare()['prepared_prompt']
        self.assertTrue(stale['stale'])
        self.assertEqual(stale['prompt'], prepared['prompt'])
