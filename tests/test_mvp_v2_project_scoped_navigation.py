"""Project-only roles can discover authorized work without gaining workspace grants."""
import unittest

from playwright.sync_api import expect
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_console as fixture
from tests import test_mvp_v2_history_pagination as history_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class ProjectScopedNavigationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FormalConsoleTests('runTest')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.own = self.f._project('可见项目')
        self.other = self.f._project('不可见项目')
        self.root = f'/api/formal/workspaces/{self.f.workspace_id}'
        for project, number in ((self.own, 3), (self.other, 5)):
            session = self.f.agent.services.tasks.create_agent_session(self.f.token, project_id=project['project_id'])
            for index in range(number):
                self.f.agent.services.tasks.create_task(self.f.token,
                    agent_session_id=session['agent_session_id'], task_type='formal_creative_draft',
                    payload_sha256=f'{index+1:064x}', expected_project_version=0)

    def actor(self, role, project=None):
        f = self.f
        result = f.identity.create_actor(f.token, workspace_id=f.workspace_id,
            project_id=(project or self.own)['project_id'], display_name='匿名'+role, auth_source='local', role=role,
            expected_workspace_version=f.projects.get_workspace(f.token, f.workspace_id)['workspace_version'])
        token = f.identity.issue_auth_session(actor_id=result['actor_id'], token='scoped-navigation-'+result['actor_id'])['auth_token']
        return result, {'X-MPlus-Agent-Auth': token}

    def test_project_editor_reviewer_viewer_lists_counts_and_cursors_stay_scoped(self):
        f = self.f
        for role in ('editor', 'reviewer', 'viewer'):
            with self.subTest(role=role):
                actor, headers = self.actor(role)
                self.assertEqual(f.client.get(self.root, headers=headers).status_code, 200)
                response = f.client.get(self.root+'/projects?summaries=true', headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual([p['project_id'] for p in response.json()['items']], [self.own['project_id']])
                self.assertNotIn('不可见项目', response.text)
                all_tasks = f.client.get(self.root+'/tasks', headers=headers)
                self.assertEqual(all_tasks.status_code, 200, all_tasks.text)
                self.assertEqual(all_tasks.json()['count'], 3)
                first = f.client.get(self.root+'/tasks?page_size=2', headers=headers)
                self.assertEqual(first.status_code, 200, first.text)
                result = first.json()
                self.assertEqual(result['matched_total'], 3)
                self.assertEqual(sum(result['workspace_status_counts'].values()), 3)
                self.assertTrue(all(p['project_id']==self.own['project_id'] for p in result['items']))
                second = f.client.get(self.root+'/tasks', headers=headers,
                    params={'page_size': 2, 'cursor': result['next_cursor']})
                self.assertEqual(second.status_code, 200, second.text)
                self.assertEqual(len(second.json()['items']), 1)
                for query in ('', '&page_size=2'):
                    denied = f.client.get(self.root+'/tasks?project_id='+self.other['project_id']+query, headers=headers)
                    self.assertEqual(denied.status_code, 403)
                    self.assertNotIn('不可见项目', denied.text)
                denied = f.client.post(self.root+'/projects', headers=headers,
                    json={'name':'不能新建工作区项目','category_id':'launch',
                          'expected_workspace_version':f.projects.get_workspace(f.token,f.workspace_id)['workspace_version']})
                self.assertEqual(denied.status_code, 403, denied.text)
                with self.assertRaisesRegex(WorkspaceServiceError, 'PERMISSION_DENIED'):
                    f.agent.services.tasks.query_workspace_tasks(headers['X-MPlus-Agent-Auth'], f.workspace_id, administrative=True)
                with f.store.connection() as c:
                    self.assertEqual(c.execute('select count(*) from role_assignments where actor_id=? and project_id is null',
                        (actor['actor_id'],)).fetchone()[0], 0)
        self.assertEqual(len(f.projects.list_projects(f.token,f.workspace_id)), 2)
        owner = f.client.get(self.root+'/tasks?page_size=2',headers=f.headers).json()
        self.assertEqual(owner['matched_total'], 8)

    def test_revoked_scope_invalidates_existing_cursor_and_workspace_access(self):
        f = self.f
        actor, headers = self.actor('viewer')
        first = f.client.get(self.root+'/tasks?page_size=2',headers=headers).json()
        with f.store.transaction() as c:
            c.execute('delete from role_assignments where actor_id=?',(actor['actor_id'],))
        for path in (self.root,self.root+'/projects?summaries=true',self.root+'/tasks?page_size=2&cursor='+first['next_cursor']):
            response=f.client.get(path,headers=headers)
            self.assertEqual(response.status_code,403,response.text)
            self.assertNotIn(self.own['project_id'],response.text)
        # Restoring only a different project never resurrects the old scope.
        with f.store.transaction() as c:
            c.execute('insert into role_assignments(assignment_id,actor_id,workspace_id,project_id,role_id,created_by_actor_id,created_at) values (?,?,?,?,?,?,?)',
                ('anonymous-restored-scope',actor['actor_id'],f.workspace_id,self.other['project_id'],'viewer',
                 f.identity.resolve_auth_session(f.token)['actor_id'],'2026-10-03T00:00:00Z'))
        response=f.client.get(self.root+'/tasks?page_size=2&cursor='+first['next_cursor'],headers=headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(all(item['project_id']==self.other['project_id'] for item in response.json()['items']))
        with f.store.transaction() as c:
            c.execute("update auth_sessions set revoked_at='2026-10-03T00:00:00Z' where actor_id=?",(actor['actor_id'],))
        self.assertEqual(f.client.get(self.root,headers=headers).status_code,401)


class ProjectScopedHistoryBrowserTests(unittest.TestCase):
    setUp = history_fixture.HistoryPaginationTests.setUp

    def test_member_discovers_only_own_history_and_returns_after_refresh(self):
        f=self.f
        actor=f.identity.create_actor(f.token,workspace_id=f.workspace_id,project_id=self.target['project_id'],
            display_name='匿名历史查看者',auth_source='local',role='viewer',
            expected_workspace_version=f.projects.get_workspace(f.token,f.workspace_id)['workspace_version'])
        token=f.identity.issue_auth_session(actor_id=actor['actor_id'],token='scoped-history-viewer')['auth_token']
        page,origin=browser_fixture.ManualReceiptBrowserTests.open_page(self,f.shell_client.app,token,f.workspace_id)
        errors=[];bad=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('response',lambda response:bad.append((response.url,response.status)) if '/api/formal/' in response.url and response.status>=400 else None)
        page.goto(origin+'/projects')
        expect(page.locator('#formal-project-list')).to_contain_text('旧项目')
        expect(page.locator('#formal-project-list')).not_to_contain_text('较新项目')
        page.goto(origin+'/tasks?view=runs')
        expect(page.locator('[data-history-list-run]')).to_have_count(3)
        selected=page.locator('[data-history-list-run]').first.get_attribute('data-history-list-run')
        page.locator('[data-history-list-run]').first.get_by_role('link',name='查看记录').click()
        expect(page.locator('[data-history-run-detail]')).to_have_attribute('data-history-run-detail',selected)
        page.reload()
        expect(page.locator('[data-history-run-detail]')).to_have_attribute('data-history-run-detail',selected)
        page.get_by_role('link',name='返回历史运行',exact=True).click()
        expect(page.locator('[data-history-list-run]')).to_have_count(3)
        self.assertEqual(errors,[]);self.assertEqual(bad,[])
        denied=f.shell_client.get(self.url,headers={'X-MPlus-Agent-Auth':token},
            params={'project_id':self.other['project_id']})
        self.assertEqual(denied.status_code,403)
        self.assertNotIn('较新项目',denied.text)
        with f.store.connection() as c:self.assertEqual(c.execute('select count(*) from tasks').fetchone()[0],0)
