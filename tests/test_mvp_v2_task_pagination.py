import unittest

from mvp_v2.formal_admin_service import FormalAdminService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_workspace_services as fixture


class TaskPaginationTests(unittest.TestCase):
    setUp = fixture.WorkspaceServiceTests.setUp
    tearDown = fixture.WorkspaceServiceTests.tearDown
    _create_project = fixture.WorkspaceServiceTests._create_project

    def seed(self, count=237):
        projects = [self._create_project(name) for name in ("甲方活动", "乙方活动")]
        sessions = [self.tasks.create_agent_session(self.owner_token, project_id=p["project_id"]) for p in projects]
        with self.store.transaction() as c:
            for i in range(count):
                s = sessions[i % 2]
                c.execute("""INSERT INTO tasks(task_id, agent_session_id, auth_session_id,
                    actor_id, project_id, task_type, payload_sha256, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (f"task_page_{i:04}", s["agent_session_id"], s["auth_session_id"], s["actor_id"],
                     s["project_id"], "formal_script_revision", "a" * 64,
                     "needs_clarification" if i % 3 == 0 else "completed",
                     "2026-09-09T08:00:00Z", f"2026-09-09T09:{i % 60:02}:00Z"))
        return projects

    def test_more_than_two_hundred_equal_time_tasks_page_without_loss(self):
        self.seed()
        ids, cursor = [], None
        while True:
            page = self.tasks.query_workspace_tasks(self.owner_token, self.workspace_id,
                                                   page_size=50, cursor=cursor)
            self.assertEqual(page["matched_total"], 237)
            self.assertEqual(page["count"], len(page["items"]))
            self.assertEqual(sum(page["workspace_status_counts"].values()), 237)
            ids.extend(row["task_id"] for row in page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(len(ids), 237)
        self.assertEqual(len(set(ids)), 237)
        self.assertEqual(ids, sorted(ids, reverse=True))
        legacy = self.tasks.list_workspace_tasks(self.owner_token, self.workspace_id)
        self.assertIsInstance(legacy, list)
        self.assertEqual(len(legacy), 100)
        self.assertEqual(legacy, sorted(legacy, key=lambda r: (r["updated_at"], r["task_id"]), reverse=True))

    def test_admin_and_employee_query_share_filters_and_totals(self):
        projects = self.seed()
        filters = dict(page_size=50, query="甲方", status="needs_clarification",
                       project_id=projects[0]["project_id"], created_from="2026-09-09", created_to="2026-09-09")
        employee = self.tasks.query_workspace_tasks(self.owner_token, self.workspace_id, **filters)
        admin = FormalAdminService(self.store).overview(self.owner_token, workspace_id=self.workspace_id, **filters)
        self.assertEqual(employee["matched_total"], 40)
        self.assertEqual(admin["matched_total"], employee["matched_total"])
        self.assertEqual([r["task_id"] for r in admin["tasks"]], [r["task_id"] for r in employee["items"]])
        self.assertEqual(len(employee["items"]), 40)
        self.assertEqual(employee["workspace_status_counts"]["needs_clarification"], 79)

    def test_cursor_cannot_cross_filter_or_workspace_boundary(self):
        projects = self.seed()
        page = self.tasks.query_workspace_tasks(self.owner_token, self.workspace_id, page_size=10)
        for filters in ({"query": "甲方"}, {"project_id": projects[0]["project_id"]},
                        {"status": "completed"}, {"cursor": "not-a-cursor"}):
            with self.subTest(filters=filters), self.assertRaises(WorkspaceServiceError):
                self.tasks.query_workspace_tasks(self.owner_token, self.workspace_id,
                    **({"page_size": 10, "cursor": page["next_cursor"]} | filters))
        with self.store.transaction() as c:
            c.execute("INSERT INTO workspaces(workspace_id,name,created_at,updated_at) VALUES ('workspace_other','另一工作区','2026-09-09','2026-09-09')")
            c.execute("""INSERT INTO role_assignments(assignment_id, actor_id, workspace_id, project_id, role_id, created_by_actor_id, created_at)
                SELECT 'assignment_other', actor_id, 'workspace_other', NULL, role_id, created_by_actor_id, created_at
                FROM role_assignments WHERE actor_id=? AND role_id='administrator' LIMIT 1""", (self.owner_id,))
        other = {"auth_token": self.owner_token, "workspace_id": "workspace_other"}
        with self.assertRaises(WorkspaceServiceError):
            self.tasks.query_workspace_tasks(other["auth_token"], other["workspace_id"],
                                            cursor=page["next_cursor"])
        with self.assertRaises(WorkspaceServiceError):
            self.tasks.query_workspace_tasks(other["auth_token"], other["workspace_id"],
                                            project_id=projects[0]["project_id"])

    def test_invalid_filters_do_not_become_an_unfiltered_query(self):
        self.seed(2)
        for values in ({"status": "typo"}, {"page_size": 201}, {"created_from": "invalid"},
                       {"created_from": "2026-09-10", "created_to": "2026-09-09"}):
            with self.subTest(values=values), self.assertRaises(WorkspaceServiceError):
                self.tasks.query_workspace_tasks(self.owner_token, self.workspace_id, **values)


class TaskActionRegressionTests(unittest.TestCase):
    def test_business_waits_and_script_audit_have_actionable_safe_projection(self):
        for status in ("needs_clarification", "proposed", "waiting_confirmation"):
            projected = TaskService.employee_task_fields({"status": status, "task_type": "formal_script_revision"}, "p")
            self.assertNotIn("刷新", projected["next_action"])
        for code in ("SCRIPT_VERSION_AUDIT_FAILED", "FORMAL_EXECUTION_FAILED"):
            projected = TaskService.employee_task_fields({"status": "blocked", "task_type": "formal_script_revision", "error_code": code}, "p")
            self.assertEqual(projected["action_href"], "/projects/p/script")
            self.assertTrue(projected["issue"])
        unknown = TaskService.employee_task_fields({"task_type": "/Users/private/internal", "status": "unknown"}, "p")
        self.assertEqual(unknown["title"], "项目任务")
        self.assertEqual(unknown["state_label"], "状态待读取")
