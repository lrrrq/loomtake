"""Waiting-plan recovery must follow current inputs without losing old evidence."""

import unittest

from mvp_v2.formal_generation_service import FormalGenerationBlocked
from tests import test_mvp_v2_formal_generation_service as formal_tests


class PlanRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = formal_tests.FormalGenerationServiceTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture._service()
        self.scope = {"workspace_id": self.fixture.workspace_id,
                      "project_id": self.fixture.project["project_id"]}

    def plan(self):
        return self.service.prepare_plan(self.fixture.token, **self.scope,
            expected_project_version=self.version(), generation_mode="text_only_draft")

    def version(self):
        return self.fixture.projects.get_project(self.fixture.token, self.scope["project_id"])["project_version"]

    def test_reconfirmed_brief_excludes_old_plan_and_recovers_new_one(self):
        old = self.plan()
        event = self.service._plan_event(self.fixture.token, old["plan_id"])
        self.fixture._confirmed_brief(expected_project_version=self.version())
        listed = self.service.list_waiting_plans(self.fixture.token, **self.scope)
        self.assertEqual(listed["items"], [])
        self.assertEqual(listed["unavailable_items"][0]["code"], "PLAN_INPUTS_STALE")
        self.assertEqual(listed["unavailable_items"][0]["status"], "stale")
        for method in (self.service.get_plan_preview, self.service.confirm_plan_and_generate):
            with self.assertRaises(FormalGenerationBlocked) as caught:
                method(self.fixture.token, **self.scope, plan_id=old["plan_id"],
                       expected_project_version=self.version())
            self.assertEqual(caught.exception.code, "PLAN_INPUTS_STALE")
        new = self.plan()
        listed = self.service.list_waiting_plans(self.fixture.token, **self.scope)
        self.assertEqual([item["plan_id"] for item in listed["items"]], [new["plan_id"]])
        self.assertEqual(new["shot_count"], old["shot_count"])
        self.assertEqual(self.service._plan_event(self.fixture.token, old["plan_id"]), event)
        self.assertEqual(self.service.services.tasks.get_task(self.fixture.token, old["plan_id"])["status"], "waiting_user")

    def test_corrupt_old_artifact_is_not_downgraded_to_input_change(self):
        old = self.plan()
        event = self.service._plan_event(self.fixture.token, old["plan_id"])
        artifact = self.service.services.artifacts.resolve_stage_artifact(
            self.service._run_id_from_ref(event["plan_ref"]), "script_plan")
        artifact.path.write_text("{}", encoding="utf-8")
        self.fixture._confirmed_brief(expected_project_version=self.version())
        listed = self.service.list_waiting_plans(self.fixture.token, **self.scope)
        self.assertEqual(listed["items"], [])
        self.assertEqual(listed["unavailable_items"][0]["status"], "blocked")
        self.assertNotEqual(listed["unavailable_items"][0]["code"], "PLAN_INPUTS_STALE")
        with self.assertRaises(FormalGenerationBlocked) as caught:
            self.service.confirm_plan_and_generate(self.fixture.token, **self.scope,
                plan_id=old["plan_id"], expected_project_version=self.version())
        self.assertNotEqual(caught.exception.code, "PLAN_INPUTS_STALE")
        with self.fixture.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM project_versions").fetchone()[0], 0)

    def test_valid_waiting_plan_remains_recoverable(self):
        plan = self.plan()
        listed = self.service.list_waiting_plans(self.fixture.token, **self.scope)
        self.assertEqual(listed, {"items": [{"plan_id": plan["plan_id"], "status": "waiting_user"}]})
        self.assertEqual(self.service.get_plan_preview(self.fixture.token, **self.scope,
            plan_id=plan["plan_id"]), plan)
