"""Image creation UI against an anonymous HTTP contract, with actual PNG previews."""
from copy import deepcopy
from email.parser import BytesParser
from email.policy import default
from hashlib import sha256
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
from threading import Thread
from urllib.parse import quote, unquote, urlparse, parse_qs
import unittest

from playwright.sync_api import expect, sync_playwright

from mvp_v2.image_providers import ZHIPU_PURPOSES

from tests.test_mvp_v2_creative_workspace_browser import _CreativeFixture
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID, UI_ROOT, _handler


class _ImageCreationFixture(_CreativeFixture):
    def __init__(self):
        super().__init__()
        self.available = True
        self.target_kind = 'formal_shot'
        self.target_id = 'shot-stable-2'
        self.creation_items = {}
        self.creations = []
        self.adoptions = []
        self.uploads = []
        self.task_reads = 0
        self.context_reads = 0
        self.preview_reads = []
        self.drop_receipt_once = False
        self.uncertain_result = False
        self.resolve_uncertain = False
        self.reject_generation = False
        self.foreign_preview = False
        self.poll_to_complete = True
        self.operation_tasks = {}
        self.prepared_prompts = {}

    def image_url(self, identity):
        return f'/api/formal/projects/{quote(PROJECT_ID, safe="")}/images/{identity}/preview?workspace_id=browser-workspace'

    def target(self):
        return {'kind': self.target_kind, 'id': self.target_id, 'label': '第 2 镜 · 客户交流'}

    def context(self):
        prepared = deepcopy(self.prepared_prompts.get(self.target_id))
        if prepared:
            prepared['stale'] = prepared['basis_revision'] != self.script_revision
        return {'available': self.available, 'unavailable_reason': '' if self.available else '尚未配置图片生成账号，请配置后重新读取。',
                'project_version': self.project_version, 'creative_revision': 3,
                'progress_revision': self.progress_revision, 'script_revision': self.script_revision,
                'target_basis': 'visual-' + str(self.script_revision), 'selection_basis': str(self.progress_revision),
                'capabilities': {'image_edit': False, 'purposes': [dict(purpose=k, **v) for k, v in ZHIPU_PURPOSES.items()]},
                'suggested_purpose': 'storyboard', 'target': self.target(), 'suggested_prompt': (prepared or {}).get('prompt', ''),
                'prepared_prompt': prepared, 'original_shot': {'visual': '现场空间与客户交流'},
                'reference_options': [{'image_id': 'reference-a', 'label': '原始现场图',
                                      'preview_url': self.image_url('reference-a')},
                                     {'image_id': 'reference-b', 'label': '人物关系参考',
                                      'preview_url': '/api/formal/projects/foreign/images/x/preview?workspace_id=browser-workspace' if self.foreign_preview else self.image_url('reference-b')}],
                'items': deepcopy(list(self.creation_items.values()))}

    def response(self, method, path):
        route = unquote(urlparse(path).path)
        if '/images/' in route and route.endswith('/preview') and any(x in route for x in ('reference-', 'created-image-')):
            self.preview_reads.append(route)
            variant = 'b' if 'created-image-' in route else 'a'
            return 200, (Path(__file__).parent / 'fixtures' / 'formal_visual_quality'
                         / f'anonymous-launch-atlas-{variant}.png').read_bytes()
        if '/image-creations' not in route:
            return super().response(method, path)
        if route.endswith('/image-creations/prepare'):
            original = self.target_id
            self.target_id = self.body['target_id']
            previous = self.prepared_prompts.get(self.target_id)
            draft = deepcopy(previous or {'status': 'ready', 'prompt': '保留现场空间与客户关系，使用自然光和克制的构图。',
                'basis_revision': self.script_revision, 'postproduction_text': [], 'conversion_notes': ['保留当前交流瞬间']})
            if self.body.get('edited_prompt'): draft['prompt'] = self.body['edited_prompt']
            if self.body.get('regenerate') or self.body.get('use_saved_prompt'): draft['basis_revision'] = self.script_revision
            draft['sha256'] = sha256(json.dumps(draft, sort_keys=True).encode()).hexdigest()
            self.prepared_prompts[self.target_id] = draft
            value = self.context()
            self.target_id = original
            return 200, value
        if route.endswith('/image-creations/context'):
            self.context_reads += 1
            original = self.target_id
            self.target_id = parse_qs(urlparse(path).query).get('target_id', [original])[0]
            value = self.context()
            self.target_id = original
            return 200, value
        if route.endswith('/image-creations') and method == 'POST':
            self.creations.append(deepcopy(self.body))
            if self.reject_generation:
                return 422, {'detail': {'code': 'INVALID_INPUT', 'message': '匿名输入不完整'}}
            operation = self.body['operation_key']
            if operation not in self.operation_tasks:
                task_id = f'image-task-{len(self.creation_items) + 1}'
                self.operation_tasks[operation] = task_id
                self.creation_items[task_id] = {'task_id': task_id,
                    'status': 'failed' if self.uncertain_result else 'executing',
                    'origin_label': '本次单张生成', 'target': {**self.target(), 'id': self.body['target_id'], 'kind': self.body['target_kind']}, 'preview_url': '',
                    'error_message': '', 'result_uncertain': self.uncertain_result,
                    'reference_previews': [{'label': '原始现场图', 'preview_url': self.image_url('reference-a')}]
                        if self.body.get('reference_image_ids') or self.uploads else []}
            item = self.creation_items[self.operation_tasks[operation]]
            if self.drop_receipt_once:
                self.drop_receipt_once = False
                return 0, None
            return 200, deepcopy(item)
        if route.endswith('/adopt'):
            self.adoptions.append(deepcopy(self.body))
            if self.body['expected_progress_revision'] is not None and self.body['expected_progress_revision'] != self.progress_revision:
                return 409, {'detail': {'code': 'IMAGE_PROGRESS_CONFLICT', 'message': '选图进度已变化'}}
            return 200, {'status': 'reference_saved' if self.target_kind == 'creative_block' else 'progress_saved',
                         'message': '参考图已采用，仍需分析和人工核对。', 'project_version': self.project_version}
        task_id = route.rsplit('/', 1)[-1]
        self.task_reads += 1
        item = self.creation_items[task_id]
        if self.poll_to_complete and (not item['result_uncertain'] or self.resolve_uncertain):
            item.update(status='completed', result_uncertain=False, preview_url=self.image_url('created-image-' + task_id))
        return 200, deepcopy(item)


class ImageCreationBrowserTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _ImageCreationFixture()
        fixture = self.fixture

        class Handler(_handler(fixture)):
            def _api(handler, method):
                fixture.body = {}
                if method in {'POST', 'PATCH'}:
                    raw = handler.rfile.read(int(handler.headers.get('Content-Length', '0')))
                    content_type = handler.headers.get('Content-Type', '')
                    if 'application/json' in content_type:
                        fixture.body = json.loads(raw or '{}')
                    elif 'multipart/form-data' in content_type:
                        message = BytesParser(policy=default).parsebytes(
                            ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + raw)
                        files = []
                        for part in message.iter_parts():
                            name = part.get_param('name', header='content-disposition')
                            content = part.get_payload(decode=True)
                            if name == 'payload':
                                fixture.body = json.loads(content)
                            elif name == 'reference_files':
                                files.append({'name': part.get_filename(), 'sha256': sha256(content).hexdigest()})
                        fixture.uploads.append(files)
                status, payload = fixture.response(method, handler.path)
                if status == 0:
                    handler.close_connection = True
                    handler.connection.shutdown(socket.SHUT_RDWR)
                    return
                raw = isinstance(payload, bytes)
                body = payload if raw else json.dumps(payload, ensure_ascii=False).encode()
                handler.send_response(status)
                handler.send_header('Content-Type', 'image/png' if raw else 'application/json')
                handler.send_header('Content-Length', str(len(body)))
                handler.end_headers(); handler.wfile.write(body)

            def _file(handler):
                if urlparse(handler.path).path.startswith('/assets/'):
                    return super()._file()
                options = json.dumps({'projectId': PROJECT_ID, 'workspaceId': 'browser-workspace',
                                      'targetKind': fixture.target_kind, 'targetId': fixture.target_id}, ensure_ascii=False)
                hook = '<button type="button" id="test-open-creation" style="position:fixed;right:20px;top:20px;z-index:20">生成图片</button>'
                hook += '<button type="button" id="test-open-batch" style="position:fixed;right:160px;top:20px;z-index:20">批量生成</button><script>'
                hook += 'document.getElementById("test-open-creation").addEventListener("click",()=>{const options=' + options + ';'
                hook += 'options.onAdopt=async()=>{document.getElementById("test-open-creation").textContent="工作台已收到采用回执";};window.MPlusImageCreation.open(options);});document.getElementById("test-open-batch").addEventListener("click",()=>window.MPlusImageCreation.openBatch({projectId:' + json.dumps(PROJECT_ID) + ',workspaceId:"browser-workspace",targetKind:' + json.dumps(fixture.target_kind) + ',targets:' + json.dumps([{'targetId': ('block-' if fixture.target_kind == 'creative_block' else 'shot-stable-') + str(i)} for i in (2, 3)]) + '}));</script>'
                body = (UI_ROOT / 'index.html').read_text().replace('__MPLUS_UI_BUILD__', 'updated-anonymous-build').replace('</body>', hook + '</body>').encode()
                handler.send_response(200); handler.send_header('Content-Type', 'text/html; charset=utf-8')
                handler.send_header('Content-Length', str(len(body))); handler.end_headers(); handler.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.playwright = sync_playwright().start(); self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True); self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        self.page.set_default_timeout(8000)
        self.page.add_init_script("localStorage.setItem('mplus.agent.auth', 'browser-session');localStorage.setItem('mplus.agent.workspace', 'browser-workspace');")
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def open(self):
        self.page.goto(f'{self.base}/projects/{PROJECT_ID}/script')
        self.page.locator('#test-open-creation').click()
        expect(self.page.get_by_role('dialog', name='生成新图')).to_be_visible()
        expect(self.page.locator('[data-creation-prompt]')).not_to_have_value('')

    def capture(self, name):
        directory = os.environ.get('MPLUS_CREATIVE_UI_ARTIFACTS')
        if directory:
            self.page.get_by_role('dialog', name='生成新图').screenshot(
                path=str(Path(directory) / f'{name}.png'), animations='disabled')

    def generate(self):
        self.page.locator('[data-creation-generate]').click()
        expect(self.page.locator('[data-creation-result-preview] img')).to_be_visible(timeout=9000)

    def test_generate_by_purpose_compare_and_adopt_preserves_single_request_and_review_boundary(self):
        self.open()
        self.assertEqual(self.fixture.creations, [])
        expect(self.page.locator('[data-creation-upload]')).to_have_count(0)
        self.page.locator('[data-creation-prompt]').fill('多格画面，人物左右关系清楚，展板写携手向前')
        self.page.locator('[data-creation-purpose]').select_option('complex')
        expect(self.page.locator('[data-creation-model]')).to_contain_text('glm-image')
        self.generate()
        self.assertEqual(len(self.fixture.creations), 1)
        self.assertEqual(self.fixture.creations[0]['purpose'], 'complex')
        self.assertIsNone(self.fixture.creations[0]['expected_creative_revision'])
        self.assertFalse(self.fixture.creations[0].get('reference_task_ids'))
        self.assertGreaterEqual(self.fixture.task_reads, 1)
        self.capture('image-creation-compare')
        self.page.locator('[data-adopt-task]').click()
        expect(self.page.locator('[data-adopt-task]')).to_have_text('已采用')
        self.assertEqual(len(self.fixture.adoptions), 1)
        self.assertEqual(self.fixture.adoptions[0]['target_id'], 'shot-stable-2')
        expect(self.page.locator('[data-creation-notice]')).to_contain_text('人工核对')
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.page.get_by_role('button', name='关闭图片生成').click()
        expect(self.page.locator('#test-open-creation')).to_have_text('工作台已收到采用回执')
        self.assertEqual(self.errors, [])

    def test_unconfigured_account_disables_generation_and_keeps_editable_input(self):
        self.fixture.available = False
        self.open()
        expect(self.page.locator('[data-creation-availability]')).to_contain_text('尚未配置')
        expect(self.page.locator('[data-creation-generate]')).to_be_disabled()
        self.page.locator('[data-creation-prompt]').fill('保留未提交的原输入')
        self.page.get_by_role('button', name='关闭图片生成').click()
        self.page.locator('#test-open-creation').click()
        expect(self.page.locator('[data-creation-prompt]')).to_have_value('保留未提交的原输入')
        self.assertEqual(self.fixture.creations, [])

    def test_current_shot_results_hide_internal_notes_and_keep_action_visible(self):
        self.fixture.prepared_prompts[self.fixture.target_id] = {
            'status': 'ready', 'prompt': '产品静置窗边桌面，自然光，右侧留白。',
            'basis_revision': self.fixture.script_revision, 'sha256': 'a' * 64,
            'postproduction_text': ['好好生活'],
            'conversion_notes': ['confirmed_facts 与 source_fields 已校验。' * 80]}
        for identity, target_id in [('current', self.fixture.target_id), ('other', 'shot-stable-1')]:
            self.fixture.creation_items[identity] = {'task_id': identity, 'status': 'completed',
                'target': {**self.fixture.target(), 'id': target_id}, 'result_uncertain': False,
                'preview_url': self.fixture.image_url('created-image-' + identity)}
        self.open()
        expect(self.page.locator('[data-creation-task]')).to_have_count(1)
        expect(self.page.locator('[data-creation-task="current"]')).to_be_visible()
        self.assertNotIn('confirmed_facts', self.page.get_by_role('dialog').inner_text())
        expect(self.page.get_by_text('好好生活', exact=True)).to_be_visible()
        expect(self.page.locator('[data-prompt-source]')).not_to_have_attribute('open', '')
        self.page.get_by_role('button', name='查看项目记录').click()
        expect(self.page.locator('[data-creation-task]')).to_have_count(2)
        expect(self.page.locator('[data-creation-task="other"] [data-adopt-task]')).to_have_count(0)
        self.page.get_by_role('button', name='返回本镜').click()
        self.capture('image-creation-clean-desktop')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.get_by_text('查看原镜头', exact=True).click()
        button = self.page.locator('[data-creation-generate]')
        box = button.bounding_box()
        self.assertGreaterEqual(box['y'], 0)
        self.assertLessEqual(box['y'] + box['height'], 844)
        self.capture('image-creation-clean-mobile')
        self.page.locator('[data-creation-prompt]').fill('手动保存的静物描述。')
        self.page.locator('[data-prompt-save]').click()
        expect(self.page.locator('[data-prompt-save]')).to_have_text('已保存')
        self.page.reload()
        self.page.locator('#test-open-creation').click()
        expect(self.page.locator('[data-creation-prompt]')).to_have_value('手动保存的静物描述。')
        expect(self.page.locator('[data-creation-task]')).to_have_count(1)
        self.assertEqual(self.fixture.creations, [])
        self.assertEqual(self.errors, [])

    def test_lost_network_receipt_retry_reuses_exact_payload(self):
        self.fixture.drop_receipt_once = True
        self.open()
        self.page.locator('[data-creation-prompt]').fill('网络回执丢失时仍保留这份画面要求')
        self.page.locator('[data-creation-generate]').click()
        expect(self.page.locator('[data-creation-retry]')).to_be_visible()
        expect(self.page.locator('[data-creation-prompt]')).to_be_disabled()
        self.assertEqual(len(self.fixture.creations), 1)
        self.page.get_by_role('button', name='关闭图片生成').click()
        self.page.locator('#test-open-creation').click()
        self.page.locator('[data-creation-retry]').click()
        expect(self.page.locator('[data-creation-result-preview] img')).to_be_visible(timeout=9000)
        self.assertEqual(len(self.fixture.creations), 2)
        self.assertEqual(self.fixture.creations[0], self.fixture.creations[1])
        self.assertEqual(len(self.fixture.creation_items), 1)
        self.assertEqual(self.errors, [])

    def test_uncertain_result_has_read_only_recovery_without_resubmission(self):
        self.fixture.uncertain_result = True
        self.open()
        self.page.locator('[data-creation-generate]').click()
        expect(self.page.locator('[data-creation-results]')).to_contain_text('结果待核实')
        expect(self.page.locator('[data-creation-generate]')).to_be_disabled()
        expect(self.page.locator('[data-creation-retry]')).to_be_hidden()
        self.page.wait_for_timeout(2200)
        self.assertEqual(self.fixture.task_reads, 0)
        self.assertEqual(len(self.fixture.creations), 1)
        self.fixture.resolve_uncertain = True
        self.page.locator('[data-refresh-task]').click()
        expect(self.page.locator('[data-creation-result-preview] img')).to_be_visible()
        self.assertEqual(len(self.fixture.creations), 1)
        self.assertEqual(self.errors, [])

    def test_unknown_previous_result_allows_a_separate_explicit_new_request(self):
        self.fixture.uncertain_result = True
        self.open()
        self.page.locator('[data-creation-generate]').click()
        expect(self.page.locator('[data-creation-results]')).to_contain_text('结果待核实')
        self.page.locator('[data-creation-new-request]').click()
        expect(self.page.locator('[data-creation-prompt]')).to_have_value('')
        expect(self.page.locator('[data-creation-generate]')).to_be_disabled()
        self.assertEqual(len(self.fixture.creations), 1)
        self.fixture.uncertain_result = False
        self.page.locator('[data-creation-prompt]').fill('新请求：生成一张会场入口参考图')
        self.page.locator('[data-creation-generate]').click()
        expect(self.page.locator('[data-creation-result-preview] img')).to_be_visible()
        self.assertEqual(len(self.fixture.creations), 2)
        self.assertNotEqual(self.fixture.creations[0]['operation_key'], self.fixture.creations[1]['operation_key'])
        expect(self.page.locator('[data-creation-results]')).to_contain_text('结果待核实')

    def test_adoption_does_not_silently_overwrite_later_progress(self):
        self.open(); self.generate()
        self.fixture.progress_revision += 1
        self.page.locator('[data-adopt-task]').click()
        expect(self.page.locator('[data-adopt-task]')).to_have_text('按最新进度采用')
        expect(self.page.locator('[data-creation-notice]')).to_contain_text('已有更新')
        self.assertEqual(self.fixture.adoptions, [])
        self.capture('image-creation-progress-changed')
        self.page.locator('[data-adopt-task]').click()
        expect(self.page.locator('[data-adopt-task]')).to_have_text('已采用')
        self.assertEqual(self.fixture.adoptions[0]['expected_progress_revision'], 1)

    def test_purpose_limit_preserves_exact_required_text(self):
        self.open()
        prompt = '必须出现的文字' * 150
        self.page.locator('[data-creation-purpose]').select_option('complex')
        self.page.locator('[data-creation-prompt]').fill(prompt)
        expect(self.page.locator('[data-creation-generate]')).to_be_disabled()
        expect(self.page.locator('[data-creation-prompt]')).to_have_value(prompt)
        self.page.locator('[data-creation-purpose]').select_option('storyboard')
        expect(self.page.locator('[data-creation-generate]')).to_be_enabled()
        self.assertEqual(self.fixture.creations, [])

    def test_rejected_input_retains_prompt_and_purpose_for_correction(self):
        self.fixture.reject_generation = True
        self.open()
        self.page.locator('[data-creation-prompt]').fill('保留原来的画面要求')
        self.page.locator('[data-creation-purpose]').select_option('rough')
        self.page.locator('[data-creation-generate]').click()
        expect(self.page.locator('[data-creation-notice]')).to_contain_text('原输入仍保留')
        expect(self.page.locator('[data-creation-prompt]')).to_have_value('保留原来的画面要求')
        expect(self.page.locator('[data-creation-prompt]')).to_be_enabled()
        self.fixture.reject_generation = False
        self.generate()
        self.assertEqual(len(self.fixture.creations), 2)
        self.assertEqual(self.fixture.creations[1]['purpose'], 'rough')
        self.assertEqual(self.errors, [])

    def open_batch(self):
        self.page.goto(f'{self.base}/projects/{PROJECT_ID}/script')
        self.page.locator('#test-open-batch').click()
        expect(self.page.locator('[data-batch-select]')).to_have_count(2)

    def test_batch_lost_receipt_locks_new_submission_and_preserves_exact_target(self):
        self.fixture.drop_receipt_once = True
        self.open_batch()
        self.page.locator('[data-batch-generate]').click()
        expect(self.page.locator('[data-batch-retry]')).to_be_visible()
        expect(self.page.locator('[data-batch-generate]')).to_be_disabled()
        self.page.locator('[data-batch-retry]').click()
        expect(self.page.locator('[data-creation-result-preview] img')).to_be_visible(timeout=9000)
        self.assertEqual(self.fixture.creations[0], self.fixture.creations[1])
        self.assertEqual(len(self.fixture.creation_items), 1)
        expect(self.page.locator('[data-batch-select="1"]')).to_be_checked()
        self.assertEqual(self.errors, [])

    def test_batch_refresh_keeps_original_prompt_basis_until_explicit_update(self):
        self.open_batch()
        self.fixture.script_revision += 1
        self.page.locator('[data-batch-refresh]').click()
        expect(self.page.locator('[data-batch-basis-update]')).to_have_count(2)
        self.page.locator('[data-batch-basis-update="0"]').click()
        self.page.locator('[data-batch-select="1"]').uncheck()
        self.page.locator('[data-batch-generate]').click()
        expect(self.page.locator('[data-creation-result-preview] img')).to_be_visible(timeout=9000)
        self.assertEqual(self.fixture.creations[0]['expected_target_basis'], 'visual-' + str(self.fixture.script_revision))
        self.assertEqual(self.errors, [])

    def test_cross_project_preview_is_not_requested_and_mobile_reference_adoption_is_scoped(self):
        self.fixture.target_kind = 'creative_block'; self.fixture.target_id = 'block-2'
        self.fixture.foreign_preview = True
        foreign_requests = []
        self.page.on('request', lambda request: foreign_requests.append(request.url) if '/projects/foreign/' in request.url else None)
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.open()
        expect(self.page.locator('[data-reference-preview]')).to_have_count(0)
        self.generate()
        self.page.locator('[data-adopt-task]').click()
        expect(self.page.locator('[data-adopt-task]')).to_have_text('已采用')
        self.assertEqual(self.fixture.creations[0]['expected_creative_revision'], 3)
        self.assertEqual(self.fixture.adoptions[0]['expected_creative_revision'], 3)
        self.assertIsNone(self.fixture.adoptions[0]['expected_progress_revision'])
        self.assertEqual(foreign_requests, [])
        self.capture('image-creation-mobile')
        self.assertEqual(self.errors, [])

    def test_initial_batch_keeps_block_kind_and_creative_revision(self):
        self.fixture.target_kind = 'creative_block'
        self.fixture.target_id = 'block-2'
        self.open_batch()
        self.page.locator('[data-batch-select="1"]').uncheck()
        self.page.locator('[data-batch-generate]').click()
        expect(self.page.locator('[data-creation-result-preview] img')).to_be_visible(timeout=9000)
        request = self.fixture.creations[0]
        self.assertEqual(request['target_kind'], 'creative_block')
        self.assertEqual(request['target_id'], 'block-2')
        self.assertEqual(request['expected_creative_revision'], 3)
        self.assertIsNone(request['expected_script_revision'])
        self.assertEqual(self.errors, [])
