"""Director material orchestration over persisted, anonymous business services.

Only model responses and worker scheduling are controlled. Project images,
analysis receipts, batch tasks, draft proposals and adoption use their existing
authorities; no live Provider or browser is exercised here.
"""
from copy import deepcopy
from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image

from mvp_v2 import creative_workbench
from mvp_v2.formal_generation_service import FormalVisionProviderPolicy
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.image_understanding import ProjectImageVisionResult
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_creative_workbench as workbench_fixture


class DirectorMaterialAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.base = workbench_fixture.CreativeWorkbenchTests("runTest")
        self.addCleanup(self.base.doCleanups)
        self.base.setUp()
        self.agent, self.generation = self.base.agent, self.base.generation
        self.fixture, self.scope = self.base.fixture, self.base.scope
        self.token = self.base.token
        self.session_id = self.base.session["agent_session_id"]
        self.calls = []
        self.on_vision = None

        def vision(path):
            self.calls.append(path)
            if self.on_vision:
                self.on_vision(path)
            return ProjectImageVisionResult(
                "pass", "匿名舞台画面", ("舞台",), ("舞台",),
                "活动空间", "舞台环境参考", "画面清晰", "未见品牌", ("画面可见舞台",),
            )

        self.generation.vision_provider_policy = FormalVisionProviderPolicy(
            invoke=vision, provider="anonymous-director-materials", model="deterministic-vision",
            prompt_version="anonymous-director-materials-v1", max_batch_size=2,
        )
        # The worker still executes its production implementation, explicitly
        # advanced by each test so reads and in-flight pauses are deterministic.
        scheduler = patch.object(creative_workbench, "_launch_material_workers")
        self.scheduler = scheduler.start()
        self.addCleanup(scheduler.stop)

    def _version(self):
        return self.generation.services.projects.get_project(
            self.token, self.scope["project_id"])["project_version"]

    def _add_images(self, count, *, offset=0):
        paired = self.generation.services.briefs.resolve_current_paired(self.token, **self.scope)
        for index in range(offset, offset + count):
            stream = BytesIO()
            Image.new("RGB", (160, 90), (20 + index * 17, 80, 140)).save(stream, format="PNG")
            self.generation.services.images.adopt_local_upload(
                self.token, **self.scope, brief_version_id=paired.brief.brief_version_id,
                brief_snapshot_id=paired.brief.brief_snapshot_id,
                expected_project_lock_version=self._version(),
                filename=f"anonymous-director-{index}.png", content=stream.getvalue(),
            )

    @staticmethod
    def _material(kind="analyze_materials"):
        instruction = {"analyze_materials": "分析本次待处理图片",
                       "pause_material_analysis": "暂停当前素材分析，保留已返回结果",
                       "resume_material_analysis": "继续原素材批次，跳过已完成图片"}[kind]
        return {"kind": kind, "target_scope": "all", "target_numbers": [],
                "fields": [], "instruction": instruction, "depends_on": []}

    def _turn(self, actions, content, key, *, session_id=None):
        self.base.provider.actions = deepcopy(actions)
        started = self.agent.start_workbench(
            self.token, session_id or self.session_id, content=content,
            context=None, operation_key=key,
        )
        self.assertEqual(started["task"]["status"], "understanding")
        return self.agent.execute_workbench(self.token, started["task"]["task_id"])

    def _batch(self, response):
        batches = response["response"]["material_analysis_batches"]
        self.assertEqual(len(batches), 1, response["response"])
        return batches[0]

    def _old_read(self, task_id, *, fresh=False):
        service = self.generation
        if fresh:
            service = self.fixture._service(
                self.generation.provider_policy, vision=self.generation.vision_provider_policy)
        return service.read_image_analysis_batch(self.token, **self.scope, task_id=task_id)

    def _execute_batch(self, task_id):
        return self.generation.execute_image_analysis_batch(self.token, task_id=task_id)

    def _business(self):
        body = self.base.repo.read()["body"]
        return {key: deepcopy(body.get(key)) for key in (
            "draft", "image_references", "patches", "inputs", "generation_basis",
            "assistant_protected_ids",
        )}

    def _assert_batch_relationship(self, child_id, parent_id, session_id=None):
        task = self.agent.services.tasks.get_task(self.token, child_id)
        self.assertEqual(task["agent_session_id"], session_id or self.session_id)
        self.assertEqual(task["project_id"], self.scope["project_id"])
        events = self.agent.services.tasks.list_task_events(self.token, child_id)
        self.assertTrue(any(event["details"].get("workbench_parent_task_id") == parent_id
                            for event in events))

    def test_no_draft_analysis_freezes_three_images_and_old_entry_reads_same_batch(self):
        self._add_images(3)
        # Remove only the anonymous seed draft to model an existing confirmed
        # Brief with materials, before the user has requested any script.
        with self.fixture.store.transaction() as connection:
            connection.execute("DELETE FROM creative_draft_heads WHERE project_id=?",
                               (self.scope["project_id"],))
        before = self._business()
        result = self._turn([self._material()], "先分析本次三张素材，不起稿。", "materials-no-draft")
        self.assertEqual(result["response"]["type"], "workbench_material_analysis", result)
        self.assertTrue(result["response"]["external_tasks_running"])
        public = self._batch(result)
        self.assertEqual(public["progress"]["total"], 3)
        child_id, parent_id = public["task_id"], result["task"]["task_id"]
        self._assert_batch_relationship(child_id, parent_id)
        session = self.agent.get_session(self.token, self.session_id)
        child = next(task for task in session['tasks'] if task['task_id'] == child_id)
        self.assertEqual(child['workbench_parent_task_id'], parent_id)
        self.assertNotIn(child_id, [message['task_id'] for message in session['messages']])
        self.assertEqual(self.calls, [])
        self._add_images(1, offset=5)
        self.assertEqual(self._old_read(child_id)["response"]["new_pending_count"], 1)
        finished = self._execute_batch(child_id)
        self.assertEqual(finished["task"]["status"], "completed")
        self.assertEqual(finished["response"]["progress"]["processed"], 3)
        self.assertEqual(len(self.calls), 3)
        fresh = self.base.new_agent().read_workbench(self.token, parent_id)
        self.assertEqual(fresh["task"]["status"], "completed")
        self.assertFalse(fresh["response"]["external_tasks_running"])
        self.assertEqual(self._batch(fresh)["items"], finished["response"]["items"])
        legacy = self._old_read(child_id, fresh=True)
        self.assertEqual(legacy["response"]["items"], finished["response"]["items"])
        self.assertEqual(legacy["response"]["new_pending_count"], 1)
        self._execute_batch(child_id)
        self.assertEqual(len(self.calls), 3, "Refreshing a finished batch must not resend images")
        self.assertEqual(self._business(), before)
        with self.fixture.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM creative_draft_heads").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM project_versions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM project_image_analysis_versions").fetchone()[0], 3)

    def test_natural_pause_keeps_inflight_receipt_and_natural_resume_skips_completed(self):
        self._add_images(3)
        before = self._business()
        result = self._turn([self._material()], "分析本次素材。", "materials-pause-start")
        parent_id, child_id = result["task"]["task_id"], self._batch(result)["task_id"]
        paused_turns = []

        def pause_first(_path):
            if len(self.calls) == 1:
                paused = self._turn([self._material("pause_material_analysis")],
                    "暂停素材分析，正在返回的这一张先保留。", "materials-pause-new-message")
                paused_turns.append(paused)
                self.assertEqual(self._batch(paused)["task_id"], child_id)
                self.assertEqual(self._batch(paused)["status"], "executing")
                self.assertTrue(paused["response"]["external_tasks_running"])

        self.on_vision = pause_first
        stopped = self._execute_batch(child_id)
        self.assertEqual(len(paused_turns), 1)
        self.assertEqual(stopped["task"]["status"], "waiting_user")
        self.assertEqual(stopped["response"]["progress"]["processed"], 1)
        receipt = stopped["response"]["items"][0]["analysis_key"]
        self._add_images(1, offset=6)
        refreshed = self.base.new_agent().read_workbench(self.token, parent_id)
        self.assertEqual(self._batch(refreshed)["task_id"], child_id)
        self.assertEqual(self._batch(refreshed)["progress"]["processed"], 1)
        self.assertEqual(self._batch(refreshed)["new_pending_count"], 1)
        self.assertFalse(refreshed["response"]["external_tasks_running"])
        self.assertTrue(self._batch(refreshed)["can_resume"])
        self.assertFalse(refreshed["response"]["can_resume"], "Parent continuation cannot bypass a paused batch")
        self.assertEqual(self._old_read(child_id, fresh=True)["response"]["items"],
                         stopped["response"]["items"])
        resumed = self._turn([self._material("resume_material_analysis")],
            "继续刚才的素材分析，不重发已经完成的图片。", "materials-resume-new-message")
        self.assertEqual(self._batch(resumed)["task_id"], child_id)
        self.assertTrue(resumed["response"]["external_tasks_running"])
        finished = self._execute_batch(child_id)
        self.assertEqual(finished["task"]["status"], "completed")
        self.assertEqual(finished["response"]["items"][0]["analysis_key"], receipt)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(set(self.calls)), 3)
        for turn in (result, resumed, paused_turns[0]):
            reread = self.base.new_agent().read_workbench(self.token, turn["task"]["task_id"])
            self.assertEqual(reread["task"]["status"], "completed")
            self.assertEqual(self._batch(reread)["task_id"], child_id)
        self.assertEqual(self._business(), before)

    def test_analysis_dependency_waits_while_independent_text_can_apply_then_resume(self):
        self._add_images(3)
        actions = [self._material(),
            {"kind": "find_images", "target_scope": "numbers", "target_numbers": [2],
             "fields": [], "instruction": "素材分析完成后给第2段找图", "depends_on": [0]},
            {"kind": "revise_text", "target_scope": "numbers", "target_numbers": [1],
             "fields": ["sound"], "instruction": "独立让第1段声音自然衔接", "depends_on": []}]
        before = self._business()
        with patch.object(self.base.library, "candidates", wraps=self.base.library.candidates) as search:
            result = self._turn(actions,
                "先分析素材，分析完成后给第2段找图；另独立让第1段声音自然衔接。",
                "materials-dependent-images-independent-text")
            self.assertEqual(result["response"]["type"], "workbench_proposal", result)
            self.assertEqual(search.call_count, 0)
            self.assertEqual({group["target_id"] for group in result["response"]["groups"]}, {"block_1"})
            self.assertTrue(result["response"]["groups"][0]["changes"])
            pending = result["response"]["pending_actions"]
            self.assertEqual([(row["action_index"], row["status"]) for row in pending],
                             [(1, "awaiting_material_analysis")])
            self.assertEqual(self._business(), before)
            parent_id = result["task"]["task_id"]
            initial_read = self.base.new_agent().read_workbench(self.token, parent_id)
            child_id = self._batch(initial_read)["task_id"]
            blocked = self.agent.resume_workbench(self.token, parent_id)
            self.assertEqual(blocked["task"]["task_id"], parent_id)
            self.assertFalse(blocked["response"]["can_resume"])
            saved = self.agent.confirm_workbench(self.token, parent_id,
                proposal_digest=result["response"]["proposal_digest"], retained_group_ids=["block_1"])
            self.assertEqual(saved["response"]["type"], "workbench_applied")
            after_text = self._business()
            self.assertNotEqual(after_text["draft"]["shots"][0]["fields"]["声音"],
                                before["draft"]["shots"][0]["fields"]["声音"])
            self.assertEqual(after_text["draft"]["shots"][1:], before["draft"]["shots"][1:])
            self._execute_batch(child_id)
            continued = self.base.new_agent().resume_workbench(self.token, parent_id)
            self.assertNotEqual(continued["task"]["task_id"], parent_id)
            self.assertEqual(continued["task"]["status"], "understanding")
            proposal = self.agent.execute_workbench(self.token, continued["task"]["task_id"])
            self.assertEqual(proposal["response"]["type"], "workbench_proposal", proposal)
            self.assertEqual([str(call.kwargs["shot_no"]) for call in search.call_args_list], ["2"])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.base.provider.editor_inputs), 1)
        self.assertEqual({group["target_id"] for group in proposal["response"]["groups"]}, {"block_2"})
        self.assertEqual(self._business(), after_text)
        fresh = self.base.new_agent().read_workbench(self.token, proposal["task"]["task_id"])
        self.assertEqual(fresh["response"], {**proposal["response"],
                                           "continuation_of_task_id": parent_id})
        self.assertEqual(self._batch(result)["task_id"], child_id)
        self.assertTrue(result["response"]["external_tasks_running"])

    def test_new_pause_and_resume_messages_keep_original_session_batch(self):
        self._add_images(3)
        first = self._turn([self._material()], "分析本会话素材。", "materials-original-session")
        first_id = self._batch(first)["task_id"]
        self._execute_batch(first_id)
        self._add_images(1, offset=7)
        other_session = self.agent.services.tasks.create_agent_session(
            self.token, project_id=self.scope["project_id"])["agent_session_id"]
        other = self._turn([self._material()], "另一个会话分析新增素材。", "materials-other-session",
                           session_id=other_session)
        other_id = self._batch(other)["task_id"]
        self.assertNotEqual(first_id, other_id)
        self._assert_batch_relationship(other_id, other["task"]["task_id"], other_session)
        for index, kind in enumerate(("pause_material_analysis", "resume_material_analysis")):
            turn = self._turn([self._material(kind)], "暂停素材分析。" if index == 0 else "继续原素材分析。",
                              "materials-original-command-" + str(index))
            self.assertEqual(self._batch(turn)["task_id"], first_id)
            self.assertEqual(self._old_read(other_id)["task"]["status"], "executing")
        self.assertEqual(len(self.calls), 3)
        self._execute_batch(other_id)
        self.assertEqual(len(self.calls), 4)

    def test_foreign_project_and_actor_cannot_attach_or_control_original_batch(self):
        self._add_images(3)
        result = self._turn([self._material()], "分析当前项目素材。", "materials-security-start")
        parent_id, child_id = result["task"]["task_id"], self._batch(result)["task_id"]
        projects = self.generation.services.projects
        workspace = projects.get_workspace(self.token, self.scope["workspace_id"])
        other = projects.create_project(self.token, workspace_id=self.scope["workspace_id"],
            name="匿名隔离项目", category_id="launch",
            expected_workspace_version=workspace["workspace_version"])
        other_session = self.agent.services.tasks.create_agent_session(
            self.token, project_id=other["project_id"])["agent_session_id"]
        identity = LocalIdentityAdapter(self.fixture.store)
        workspace = projects.get_workspace(self.token, self.scope["workspace_id"])
        actor = identity.create_actor(self.token, workspace_id=self.scope["workspace_id"],
            display_name="匿名素材同事", auth_source="local", role="reviewer",
            expected_workspace_version=workspace["workspace_version"], project_id=self.scope["project_id"])
        other_token = identity.issue_auth_session(actor_id=actor["actor_id"])["auth_token"]
        before = self.base.database()
        with self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
            self.generation.read_image_analysis_batch(self.token,
                workspace_id=self.scope["workspace_id"], project_id=other["project_id"], task_id=child_id)
        with self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
            self.generation.start_image_analysis_batch(self.token, **self.scope,
                expected_project_version=self._version(), idempotency_key="foreign-material-session",
                agent_session_id=other_session, workbench_parent_task_id=parent_id)
        for command in (
            lambda: self.agent.read_workbench(other_token, parent_id),
            lambda: self.generation.read_image_analysis_batch(other_token, **self.scope, task_id=child_id),
            lambda: self.generation.pause_image_analysis_batch(other_token, task_id=child_id),
            lambda: self.generation.resume_image_analysis_batch(other_token, task_id=child_id),
        ):
            with self.assertRaises(WorkspaceServiceError) as rejected:
                command()
            self.assertEqual(rejected.exception.code, "PERMISSION_DENIED")
        self.assertEqual(self.base.database(), before)
        self.assertEqual(self._old_read(child_id)["task"]["status"], "executing")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
