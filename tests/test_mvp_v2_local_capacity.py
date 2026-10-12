"""Bounded local HTTP workload: five identities, two creative tasks, >1000 histories.

Measures the local application with controlled model latency. It does not assert
production capacity, formal-render throughput, or the 500 MB upload target.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import resource
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from playwright.sync_api import expect
from mvp_v2 import local_ui
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
from mvp_v2.version_delivery import VersionDeliveryService
from tests import test_mvp_v2_creative_draft_api as fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class LocalCapacityTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_five_users_two_tasks_preserve_isolation_under_bounded_history_load(self):
        support = fixture.CreativeDraftApiTests('runTest')
        support.setUp(); self.addCleanup(support.doCleanups)
        f = support.fixture
        projects = [support.project, f._project('匿名并发项目乙')]
        for project in projects:
            f._confirm_formal_brief(project['project_id'])
        identities = []
        for index in range(5):
            project = projects[index % 2]
            actor = f.identity.create_actor(f.token, workspace_id=f.workspace_id,
                project_id=project['project_id'], display_name=f'匿名并发成员{index}',
                auth_source='local', role='editor' if index < 2 else 'viewer',
                expected_workspace_version=f.projects.get_workspace(f.token, f.workspace_id)['workspace_version'])
            token = f.identity.issue_auth_session(actor_id=actor['actor_id'], token=f'anonymous-capacity-{index}')['auth_token']
            identities.append({'token': token, 'project_id': project['project_id'], 'actor_id': actor['actor_id']})
        history = f.root / 'load-history'; counts = {p['project_id']: 0 for p in projects}
        for index in range(1103):
            project = projects[0 if index < 1003 else 1]['project_id']
            target = history / 'launch' / 'runs' / f'run-load-{index:04d}' / 'run_report.json'
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({'run_id': target.parent.name, 'project_id': project,
                'exit_code': 0, 'status': 'completed'}))
            counts[project] += 1
        for name, value in [('history_roots', [history]), ('load_runtime_config', {
                'legacy_catalog_path': str(history/'absent.json'), 'cache_root': str(f.root/'cache')})]:
            guard = patch('mvp_v2.local_ui.' + name, return_value=value)
            guard.start(); self.addCleanup(guard.stop)
        release = threading.Event(); begin_stream = threading.Event(); both_started = threading.Event()
        self.addCleanup(release.set); self.addCleanup(begin_stream.set)
        lock = threading.Lock(); calls = []; active = 0; max_active = 0
        def provider(prompt, model, limit, emit):
            nonlocal active, max_active
            context = json.loads(prompt.split('\n项目上下文：', 1)[1])
            with lock:
                calls.append(context['project_name']); active += 1; max_active = max(max_active, active)
                if active == 2: both_started.set()
            try:
                if not begin_stream.wait(30): raise RuntimeError('bounded load did not start')
                blocks = [{'段落': f'匿名段落{i+1}', '画面描述': context['project_name'] + f'第{i+1}段现场画面',
                           '声音': '匿名现场环境声', '时长': '建议3秒'} for i in range(20)]
                text = json.dumps({'blocks': blocks}, ensure_ascii=False)
                for index in range(0, len(text), 160):
                    emit(text[index:index+160]); time.sleep(.005)
                if not release.wait(90): raise RuntimeError('bounded load release timed out')
                return text
            finally:
                with lock: active -= 1
        f.generation.creative_stream_provider = provider
        artifacts = f.agent.services.artifacts
        scripts = FormalScriptWorkspaceService(FormalScriptWorkspaceServices(store=f.store,
            projects=f.projects, delivery=VersionDeliveryService(f.store, artifacts), commands=None,
            artifacts=artifacts), runs_root=f.root/'runs', source_root=f.root/'source')
        app = local_ui.create_app(agent_api=f.agent, formal_script_service=scripts,
            formal_generation_service=f.generation, formal_brief_adapter=f.adapter,
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, identities[0]['token'], f.workspace_id)
        records = []; transaction_acquisition = []; scans = []
        original_scan = local_ui.scan_history
        def observed_scan(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original_scan(*args, **kwargs)
            finally:
                with lock: scans.append(time.perf_counter() - started)
        scan_guard = patch.object(local_ui, 'scan_history', observed_scan)
        scan_guard.start(); self.addCleanup(scan_guard.stop)
        original_transaction = f.store.transaction
        @contextmanager
        def observed_transaction():
            started = time.perf_counter()
            with original_transaction() as connection:
                with lock: transaction_acquisition.append(time.perf_counter() - started)
                yield connection
        guard = patch.object(f.store, 'transaction', observed_transaction)
        guard.start(); self.addCleanup(guard.stop)
        deadline = time.monotonic() + 180
        def request(identity, path, method='GET', body=None, phase='verify', expected=200):
            self.assertLess(time.monotonic(), deadline, 'local workload stop deadline')
            req = Request(origin + path, data=None if body is None else json.dumps(body).encode(), method=method,
                headers={'X-MPlus-Agent-Auth': identity['token'], 'Origin': origin, 'Content-Type': 'application/json'})
            start = time.perf_counter()
            try:
                with urlopen(req, timeout=12) as response: status, payload = response.status, response.read()
            except HTTPError as exc: status, payload = exc.code, exc.read()
            elapsed = time.perf_counter() - start
            with lock: records.append({'phase': phase, 'route': path.split('?')[0], 'method': method,
                'status': status, 'seconds': elapsed, 'response_bytes': len(payload), 'expected_status': expected})
            self.assertEqual(status, expected, payload.decode()[:500])
            return json.loads(payload)
        def draft_path(identity, action=''):
            return f"/api/formal/projects/{identity['project_id']}/creative-draft{('/'+action) if action else ''}?workspace_id={f.workspace_id}"
        def browse(identity, repeats, phase):
            for _ in range(repeats):
                draft = request(identity, draft_path(identity), phase=phase)
                self.assertEqual(draft['project_id'], identity['project_id'])
                rows = request(identity, f'/api/formal/workspaces/{f.workspace_id}/legacy-runs?project_id={identity["project_id"]}&page_size=20', phase=phase)
                self.assertEqual(rows['matched_total'], counts[identity['project_id']])
                self.assertEqual(len(rows['items']), 20)
                self.assertTrue(all(row['project_id'] == identity['project_id'] for row in rows['items']))
        protected_tables = ('project_versions', 'version_reviews', 'version_publications', 'document_images', 'project_images', 'brief_confirmations')
        def protected_rows():
            with f.store.connection() as c:
                return {name: [tuple(row) for row in c.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in protected_tables}
        protected = protected_rows(); resources_before = resource.getrusage(resource.RUSAGE_SELF)
        completed = False
        try:
            for identity in identities: browse(identity, 1, 'sequential_baseline')
            def start(identity):
                current = request(identity, draft_path(identity))
                return request(identity, draft_path(identity, 'generate'), 'POST', {
                    'expected_project_version': current['project_version'], 'expected_revision': current['revision'],
                    'instruction': '按已确认需求创作', 'use_confirmed_brief': True,
                    'operation_key': 'local-capacity-'+identity['actor_id']})
            with ThreadPoolExecutor(max_workers=2) as pool:
                started = list(pool.map(start, identities[:2]))
            self.assertTrue(both_started.wait(15), 'two provider invocations did not overlap')
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(browse, identity, 8, 'five_users_two_tasks') for identity in identities]
                begin_stream.set()
                for future in futures: future.result(timeout=90)
            release.set()
            for identity in identities[:2]:
                for _ in range(100):
                    current = request(identity, draft_path(identity))
                    if current['task']['status'] == 'completed': break
                    self.assertNotEqual(current['task']['status'], 'failed', current)
                    time.sleep(.05)
                self.assertEqual(current['task']['status'], 'completed')
                self.assertEqual(len(current['blocks']), 20)
            def edit(identity):
                current = request(identity, draft_path(identity))
                body = {'expected_project_version': current['project_version'], 'expected_revision': current['revision'],
                    'expected_draft_revision': current['draft_revision'], 'action': 'patch',
                    'block_id': current['blocks'][0]['block_id'], 'field': '声音',
                    'value': identity['project_id']+'并发保存', 'operation_key': 'bounded-edit-'+identity['actor_id']}
                saved = request(identity, draft_path(identity, 'changes'), 'POST', body, phase='two_writes')
                replay = request(identity, draft_path(identity, 'changes'), 'POST', body, phase='two_writes')
                self.assertEqual(saved['revision'], replay['revision'])
                return saved
            with ThreadPoolExecutor(max_workers=2) as pool: saved = list(pool.map(edit, identities[:2]))
            # Normal browser readback under a valid project editor identity.
            page.goto(origin+'/projects/'+identities[0]['project_id']+'/script')
            expect(page.locator('[data-first-field="画面描述"]')).to_have_count(20)
            expect(page.locator('[data-first-field="声音"]').first).to_have_value(identities[0]['project_id']+'并发保存')
            page.reload()
            expect(page.locator('[data-first-field="声音"]').first).to_have_value(identities[0]['project_id']+'并发保存')
            for identity in identities:
                current = request(identity, draft_path(identity))
                self.assertEqual(current['blocks'][0]['fields']['声音'], identity['project_id']+'并发保存')
            viewer = identities[2]
            request(viewer, draft_path(viewer, 'changes'), 'POST', body={
                'expected_project_version': saved[0]['project_version'], 'expected_revision': saved[0]['revision'],
                'expected_draft_revision': saved[0]['draft_revision'], 'action': 'patch',
                'block_id': saved[0]['blocks'][0]['block_id'], 'field': '声音', 'value': '不能越权',
                'operation_key': 'viewer-forbidden-change'}, expected=403)
            foreign = {**identities[0], 'project_id': identities[1]['project_id']}
            request(foreign, draft_path(foreign), expected=403)
            self.assertEqual(protected_rows(), protected)
            self.assertEqual(len(calls), 2); self.assertEqual(max_active, 2)
            with f.store.connection() as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 2)
                self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
            completed = True
        finally:
            release.set(); begin_stream.set()
            output = os.environ.get('MPLUS_LOCAL_CAPACITY_EVIDENCE_ROOT')
            if output:
                destination = Path(output); destination.mkdir(parents=True, exist_ok=True)
                def distribution(values):
                    ordered = sorted(values)
                    return {'samples': len(ordered), 'p50_seconds': ordered[max(0, math.ceil(len(ordered)*.5)-1)] if ordered else None,
                        'p95_seconds': ordered[max(0, math.ceil(len(ordered)*.95)-1)] if ordered else None,
                        'max_seconds': max(ordered) if ordered else None}
                now = resource.getrusage(resource.RUSAGE_SELF)
                result = {'completed': completed, 'identities': 5, 'concurrent_creative_tasks': max_active,
                    'histories': 1103, 'blocks_per_project': 20, 'provider_calls': len(calls),
                    'requests': records, 'unexpected_statuses': [r for r in records if r['status']!=r['expected_status']],
                    'latency': {phase: distribution([r['seconds'] for r in records if r['phase']==phase]) for phase in {r['phase'] for r in records}},
                    'transaction_acquisition_including_connect': distribution(transaction_acquisition),
                    'history_scans': distribution(scans),
                    'process_resource_usage': {'peak_rss_native_units': now.ru_maxrss, 'user_cpu_seconds': now.ru_utime-resources_before.ru_utime,
                        'system_cpu_seconds': now.ru_stime-resources_before.ru_stime, 'input_blocks_delta': now.ru_inblock-resources_before.ru_inblock,
                        'output_blocks_delta': now.ru_oublock-resources_before.ru_oublock},
                    'stop_conditions': {'wall_seconds': 180, 'client_timeout_seconds': 12, 'parallel_read_requests': 80, 'unexpected_status': 'stop'},
                    'boundary': 'Local in-process HTTP server and clients; process resource metrics include test harness. Model output/delay controlled. No production load, paid calls, upload limit change, large-file target, formal-render throughput or production SLA claim.'}
                (destination/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
                (destination/'fixture-db-path.txt').write_text(str(f.store.database_path)+'\n')
                with f.store.connection() as source:
                    import sqlite3
                    with sqlite3.connect(destination/'workspace.sqlite3') as target: source.backup(target)
