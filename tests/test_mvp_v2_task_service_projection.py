import unittest

from mvp_v2.task_service import TaskService
from tests import test_mvp_v2_workspace_services as workspace_fixture


class TaskServiceProjectionTests(unittest.TestCase):
    def test_completed_task_copy_and_destination_follow_task_type(self):
        project_id = "project_anonymous"
        cases = {
            "formal_generation_plan": (
                "规划任务已完成，请核对规划与实际结果",
                "核对规划",
                f"/projects/{project_id}/plan",
            ),
            "formal_script_revision": (
                "脚本修改已完成，请查看当前草稿或版本",
                "查看脚本",
                f"/projects/{project_id}/script",
            ),
            "formal_image_revision": (
                "图片修改已完成，请继续逐镜确认",
                "查看逐镜配图",
                f"/projects/{project_id}/shot-images",
            ),
            "formal_image_version": (
                "逐镜配图已保存，请继续审核确认",
                "查看逐镜配图",
                f"/projects/{project_id}/shot-images",
            ),
            "formal_default_image_adoption": (
                "类别默认图片已加入项目素材，请继续逐镜确认",
                "继续逐镜确认",
                f"/projects/{project_id}/shot-images",
            ),
            "formal_historical_continuation": (
                "历史 Brief 续作已完成，请查看目标项目并继续确认",
                "查看项目",
                f"/projects/{project_id}/overview",
            ),
            "formal_historical_review_import": (
                "历史复核已导入，请核对审核结果",
                "进入审核",
                f"/projects/{project_id}/review",
            ),
        }

        for task_type, expected in cases.items():
            with self.subTest(task_type=task_type):
                projection = TaskService.employee_task_fields(
                    {"task_type": task_type, "status": "completed"},
                    project_id,
                )
                self.assertEqual(
                    (
                        projection["next_action"],
                        projection["action_label"],
                        projection["action_href"],
                    ),
                    expected,
                )

        default_images = TaskService.employee_task_fields(
            {"task_type": "formal_default_image_adoption", "status": "completed"},
            project_id,
        )
        self.assertNotIn("脚本版本", default_images["next_action"])

    def test_unknown_completed_task_does_not_claim_script_generation(self):
        projection = TaskService.employee_task_fields(
            {"task_type": "future_adapter_task", "status": "completed"},
            "project_anonymous",
        )

        self.assertEqual(projection["next_action"], "任务已完成，请查看项目最新状态")
        self.assertEqual(projection["action_href"], "/projects/project_anonymous/overview")
        self.assertNotIn("脚本版本", projection["next_action"])


class TaskResultEvidenceTests(unittest.TestCase):
    setUp = workspace_fixture.WorkspaceServiceTests.setUp
    tearDown = workspace_fixture.WorkspaceServiceTests.tearDown
    _create_project = workspace_fixture.WorkspaceServiceTests._create_project
    _create_version = workspace_fixture.WorkspaceServiceTests._create_version

    def _complete_plan_task(self, project_id, details):
        project = self.projects.get_project(self.owner_token, project_id)
        session = self.tasks.create_agent_session(self.owner_token, project_id=project_id)
        task = self.tasks.create_task(
            self.owner_token, agent_session_id=session["agent_session_id"],
            task_type="formal_generation_plan", payload_sha256="a" * 64,
            expected_project_version=project["project_version"],
        )
        executing = self.tasks.transition_task(
            self.owner_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="executing",
        )
        self.tasks.transition_task(
            self.owner_token, task_id=task["task_id"],
            expected_task_version=executing["task_version"], status="completed",
            result_ref=f"artifact://task/{task['task_id']}/result", details=details,
        )
        return task["task_id"]

    def test_persisted_trial_completion_routes_to_trial_without_claiming_formal_version(self):
        project = self._create_project()
        task_id = self._complete_plan_task(project["project_id"], {"action": "trial_template"})
        collections = (
            self.tasks.list_project_tasks(self.owner_token, project["project_id"]),
            self.tasks.list_workspace_tasks(self.owner_token, self.workspace_id),
            self.tasks.query_workspace_tasks(self.owner_token, self.workspace_id)["items"],
        )
        for rows in collections:
            with self.subTest(collection=rows):
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["result_kind"], "trial")
                self.assertEqual(rows[0]["action_label"], "查看试排")
                self.assertEqual(rows[0]["action_href"], f"/projects/{project['project_id']}/plan")
                self.assertIn("尚未创建正式版本", rows[0]["next_action"])
        self.assertEqual(self.tasks.get_task(self.owner_token, task_id)["status"], "completed")
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM project_versions").fetchone()[0], 0)

    def test_formal_result_requires_existing_version_in_the_same_project(self):
        project, _, version = self._create_version()
        other = self._create_project("匿名其他项目")
        project_id = project["project_id"]
        same_version = self._complete_plan_task(project_id, {"version_id": version["version_id"]})
        missing_version = self._complete_plan_task(project_id, {"version_id": "version_missing"})
        plan_only = self._complete_plan_task(project_id, {"action": "generate_formal"})
        foreign_version = self._complete_plan_task(other["project_id"], {"version_id": version["version_id"]})
        for rows in (
            self.tasks.list_workspace_tasks(self.owner_token, self.workspace_id),
            self.tasks.query_workspace_tasks(self.owner_token, self.workspace_id)["items"],
        ):
            indexed = {row["task_id"]: row for row in rows}
            self.assertEqual(indexed[same_version]["result_kind"], "version")
            self.assertEqual(indexed[same_version]["action_href"], f"/projects/{project_id}/script")
            self.assertEqual(indexed[same_version]["next_action"], "脚本版本已生成并保存在当前项目")
            for task_id in (missing_version, plan_only, foreign_version):
                with self.subTest(task_id=task_id):
                    self.assertEqual(indexed[task_id]["result_kind"], "plan")
                    self.assertNotIn("脚本版本已生成", indexed[task_id]["next_action"])
        project_rows = self.tasks.list_project_tasks(self.owner_token, project_id)
        self.assertEqual(sorted(row["result_kind"] for row in project_rows), ["plan", "plan", "version"])


if __name__ == "__main__":
    unittest.main()
