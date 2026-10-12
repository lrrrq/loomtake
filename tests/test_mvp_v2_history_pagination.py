"""History scope precedes pagination, including older-than-global-window records."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from mvp_v2.cloud_runtime import MemoryCloudRunRepository
from mvp_v2.local_ui import scan_history, ArtifactRegistry
from tests import test_mvp_v2_formal_console as fixture


class HistoryPaginationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.FormalConsoleTests('runTest'); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.target=self.f._project('旧项目')
        self.other=self.f._project('较新项目')
        self.root=self.f.root/'history'
        self.root.mkdir()
        for index in range(1103):
            run_id=f'run-history-{index:04d}'
            path=self.root/'launch'/'runs'/run_id/'run_report.json'; path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'run_id':run_id,'project_id':(self.target if index<3 else self.other)['project_id'],
                'exit_code':0,'status':'completed'}))
            os.utime(path, (100 if index<3 else 200+index,)*2)
        for name,value in [('history_roots',[self.root]),('load_runtime_config',{'legacy_catalog_path':str(self.root/'absent.json')})]:
            guard=patch('mvp_v2.local_ui.'+name,return_value=value);guard.start();self.addCleanup(guard.stop)
        self.url=f'/api/formal/workspaces/{self.f.workspace_id}/legacy-runs'

    def get(self, params=None, run_id='', headers=None):
        return self.f.shell_client.get(self.url+('/'+run_id if run_id else ''),params=params or {},headers=headers or self.f.headers)

    def test_old_project_pages_detail_and_scope_bound_cursor(self):
        params={'project_id':self.target['project_id'],'page_size':2}
        first=self.get(params);self.assertEqual(first.status_code,200,first.text)
        body=first.json();self.assertEqual(body['matched_total'],3)
        self.assertEqual([i['run_id'] for i in body['items']], ['run-history-0002','run-history-0001'])
        second=self.get({**params,'cursor':body['next_cursor']}).json()
        self.assertEqual([i['run_id'] for i in second['items']], ['run-history-0000'])
        self.assertIsNone(second['next_cursor'])
        detail=self.get(run_id='run-history-0000')
        self.assertEqual(detail.status_code,200,detail.text)
        self.assertEqual(detail.json()['project_id'],self.target['project_id'])
        mismatch=self.get({**params,'project_id':self.other['project_id'],'cursor':body['next_cursor']})
        self.assertEqual(mismatch.status_code,422)
        self.assertEqual(self.get({'cursor':'invalid!'}).status_code,422)
        actor=self.f.identity.create_actor(self.f.token,workspace_id=self.f.workspace_id,project_id=self.other['project_id'],
            display_name='另一项目查看者',auth_source='local',role='viewer',expected_workspace_version=self.f.projects.get_workspace(self.f.token,self.f.workspace_id)['workspace_version'])
        token=self.f.identity.issue_auth_session(actor_id=actor['actor_id'],token='history-other-viewer')['auth_token']
        denied=self.get(run_id='run-history-0000',headers={'X-MPlus-Agent-Auth':token})
        self.assertEqual(denied.status_code,403)
        self.assertNotIn('旧项目',denied.text)

    def test_cloud_older_than_thousand_is_merged_and_exactly_addressed(self):
        repo=MemoryCloudRunRepository()
        for index in range(1101):
            repo.register_legacy_run({'run_id':f'cloud-history-{index:04d}','project_name':'匿名云归档','status':'succeeded',
                'finished_at':f'2026-09-{1 if index==0 else 2:02d}T00:00:00Z',
                'artifact_availability':'metadata_only','source_manifest_sha256':'1'*64,
                'catalog_ref':f"cos://anonymous/catalog?sha256={'2'*64}",'catalog_sha256':'2'*64,'delivery_files':[]})
        self.assertEqual(len(repo.list_legacy_runs(limit=None)),1101)
        self.assertEqual(repo.list_legacy_runs(limit=None,run_id='cloud-history-0000')[0]['run_id'],'cloud-history-0000')
        self.f.projects.bind_run(self.f.token,project_id=self.target['project_id'],run_id='cloud-history-0000',
            source_kind='legacy',manifest_ref='artifact://run/cloud-history-0000/manifest',manifest_sha256='1'*64,
            expected_project_version=self.target['project_version'])
        self.f.shell_client.app.state.cloud_run_repository=repo
        body=self.get({'project_id':self.target['project_id'],'query':'cloud-history-0000'}).json()
        self.assertEqual(body['matched_total'],1)
        self.assertEqual(body['items'][0]['run_id'],'cloud-history-0000')
        detail=self.get(run_id='cloud-history-0000')
        self.assertEqual(detail.status_code,200,detail.text)
        self.assertEqual(detail.json()['project_id'],self.target['project_id'])


class HistoryPaginationBrowserTests(unittest.TestCase):
    setUp=HistoryPaginationTests.setUp

    def test_second_page_detail_returns_to_same_filter_and_record(self):
        from playwright.sync_api import expect
        from tests import test_mvp_v2_manual_receipt_browser as browser_fixture
        page,origin=browser_fixture.ManualReceiptBrowserTests.open_page(self,self.f.shell_client.app,self.f.token,self.f.workspace_id)
        page.goto(origin+'/tasks?view=runs&project_id='+self.other['project_id']+'&query=run-history')
        expect(page.locator('[data-history-list-run]')).to_have_count(50)
        first=page.locator('[data-history-list-run]').first.get_attribute('data-history-list-run')
        page.get_by_role('link',name='下一页',exact=True).click()
        expect(page.get_by_role('link',name='返回第一页',exact=True)).to_be_visible()
        expect(page.locator('[data-history-list-run]')).to_have_count(50)
        second=page.locator('[data-history-list-run]').first.get_attribute('data-history-list-run')
        self.assertNotEqual(first,second)
        list_url=page.url
        row=page.locator('[data-history-list-run]').nth(5)
        chosen=row.get_attribute('data-history-list-run')
        row.get_by_role('link',name='查看记录').click()
        expect(page.locator('[data-history-run-detail]')).to_have_attribute('data-history-run-detail',chosen)
        page.get_by_role('link',name='返回历史运行',exact=True).click()
        expect(page.locator('[data-history-list-run]')).to_have_count(50)
        from urllib.parse import urlparse,parse_qs
        old=parse_qs(urlparse(list_url).query);new=parse_qs(urlparse(page.url).query)
        for key in ('project_id','query','cursor'): self.assertEqual(old[key],new[key])
        expect(page.locator(f'[data-history-list-run="{chosen}"] a')).to_be_focused()
        with self.f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],0)


class OverlappingHistoryReadsTests(unittest.TestCase):
    setUp = HistoryPaginationTests.setUp

    def test_overlapping_scans_keep_per_request_permissions_and_do_not_cache_later_reads(self):
        from concurrent.futures import Future, ThreadPoolExecutor
        import threading
        entered = threading.Event(); release = threading.Event(); waiter = threading.Event()
        self.addCleanup(release.set)
        f = self.f
        actor = f.identity.create_actor(f.token, workspace_id=f.workspace_id,
            project_id=self.target['project_id'], display_name='匿名范围查看者', auth_source='local', role='viewer',
            expected_workspace_version=f.projects.get_workspace(f.token,f.workspace_id)['workspace_version'])
        token = f.identity.issue_auth_session(actor_id=actor['actor_id'],token='overlap-project-viewer')['auth_token']
        headers = {'X-MPlus-Agent-Auth':token}
        calls = []
        class ObservedFuture(Future):
            def result(self, timeout=None):
                waiter.set()
                return super().result(timeout)
        def slow_scan(*args, **kwargs):
            calls.append(True); entered.set()
            if not release.wait(10): raise TimeoutError('overlap test release')
            return scan_history(*args, **kwargs)
        with patch('mvp_v2.local_ui.Future',ObservedFuture), patch('mvp_v2.local_ui.scan_history',side_effect=slow_scan):
            with ThreadPoolExecutor(max_workers=2) as pool:
                owner=pool.submit(f.shell_client.get,self.url,headers=f.headers)
                self.assertTrue(entered.wait(5))
                member=pool.submit(f.shell_client.get,self.url,headers=headers)
                self.assertTrue(waiter.wait(5),'second request did not join active scan')
                release.set()
                all_rows=owner.result(timeout=10); own_rows=member.result(timeout=10)
        self.assertEqual(len(calls),1)
        self.assertEqual(all_rows.status_code,200,all_rows.text)
        self.assertEqual(all_rows.json()['matched_total'],1103)
        self.assertEqual(own_rows.status_code,200,own_rows.text)
        self.assertEqual(own_rows.json()['matched_total'],3)
        self.assertTrue(all(row['project_id']==self.target['project_id'] for row in own_rows.json()['items']))
        self.assertNotIn('较新项目',own_rows.text)
        path=self.root/'launch'/'runs'/'run-history-new'/'run_report.json';path.parent.mkdir()
        path.write_text(json.dumps({'run_id':'run-history-new','project_id':self.target['project_id'],'exit_code':0,'status':'completed'}))
        latest=f.shell_client.get(self.url,headers=headers)
        self.assertEqual(latest.status_code,200,latest.text)
        self.assertEqual(latest.json()['matched_total'],4)
        with f.store.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],0)

    def test_failed_scan_is_shared_and_next_request_can_recover(self):
        from concurrent.futures import Future, ThreadPoolExecutor
        import threading
        entered=threading.Event();release=threading.Event();waiter=threading.Event()
        self.addCleanup(release.set)
        class ObservedFuture(Future):
            def result(self,timeout=None):
                waiter.set();return super().result(timeout)
        def failing_scan(*args,**kwargs):
            entered.set()
            if not release.wait(10):raise TimeoutError('failed scan release')
            raise RuntimeError('anonymous scan unavailable')
        with patch('mvp_v2.local_ui.Future',ObservedFuture),patch('mvp_v2.local_ui.scan_history',side_effect=failing_scan):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(self.f.shell_client.get,self.url,headers=self.f.headers)
                self.assertTrue(entered.wait(5))
                second=pool.submit(self.f.shell_client.get,self.url,headers=self.f.headers)
                self.assertTrue(waiter.wait(5));release.set()
                for response in (first,second):
                    with self.assertRaisesRegex(RuntimeError,'anonymous scan unavailable'):response.result(timeout=10)
        recovered=self.f.shell_client.get(self.url,headers=self.f.headers)
        self.assertEqual(recovered.status_code,200,recovered.text)
        self.assertEqual(recovered.json()['matched_total'],1103)
