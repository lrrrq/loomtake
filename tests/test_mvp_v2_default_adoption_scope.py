"""Selected library adoption must not run semantic matching for the whole script."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp_v2 import formal_default_images
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_default_image_working_draft as working_fixture


class DefaultAdoptionScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = working_fixture.DefaultImageWorkingDraftTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.library = self.fixture.library
        self.token = self.fixture.token
        self.scope = self.fixture.scope
        self.project = self.fixture.project
        self.project_version = self.fixture.current["project_version"]
        self.shots = self.fixture.script["shots"]
        self.assertGreater(len(self.shots), 6)
        self.selected_nos = ("2", "6")
        self.revision = 0
        for number in self.selected_nos:
            result = self.f.base.workspace.apply_table_change(self.token, **self.scope,
                expected_project_version=self.project_version, expected_revision=self.revision,
                shot_no=number, shot_id=self.shots[int(number) - 1]["shot_id"], field="visual",
                value=f"第{number}镜外场入口人群在展板前交流", idempotency_key="scope-edit-" + number)
            self.revision = result["revision"]
        self.candidates = {}
        for number in self.selected_nos:
            page = self.library.candidates(self.token, **self.scope,
                expected_project_version=self.project_version, shot_no=number)
            self.candidates[number] = page["shots"][0]["candidates"][0]
        self.semantic_targets = []
        chosen = {candidate["candidate_key"] for candidate in self.candidates.values()}

        def writer(prompt, _model, _tokens):
            payload = json.loads(prompt.split("\nINPUT:", 1)[1])
            self.semantic_targets.append(str(payload["slot"]["镜号"]))
            return json.dumps({"matches": [
                {"image_id": candidate["image_id"], "score": 99,
                 "hits": ["人群交流"], "missing": [], "conflicts": [],
                 "reason": "匿名来源图中可见人群交流，仍需人工核对", "brand_checks": []}
                for candidate in payload["candidates"] if candidate["image_id"] in chosen
            ]}, ensure_ascii=False)

        self.library.semantic_policy = SimpleNamespace(writer=writer,
            writer_provider="anonymous-scope-regression", writer_model="anonymous-scope-regression")
        self.library.semantic_cache_root = None
        # Measure the real cold ranking and semantic path, not the warm picker.
        self.library._library_query_cache.clear()

    def request(self, key="selected-adoption"):
        return dict(**self.scope, agent_session_id="", expected_project_version=self.project_version,
            expected_script_revision=self.revision,
            allowed_shot_ids=[row["shot_id"] for row in self.shots],
            selections=[{"shot_no": number, "candidate_key": self.candidates[number]["candidate_key"]}
                        for number in self.selected_nos], idempotency_key=key)

    def propose(self, key="selected-adoption"):
        proposal = self.library.propose(self.token, **self.request(key))
        parameters = next(event["details"]["internal_formal_parameters"]
            for event in self.library.tasks.list_task_events(self.token, task_id=proposal["task_id"])
            if "internal_formal_parameters" in event["details"])
        return proposal, parameters

    def adoption_tasks(self):
        with self.f.store.connection() as connection:
            return connection.execute("SELECT count(*) FROM tasks WHERE task_type='formal_default_image_adoption'").fetchone()[0]

    def advance_working_revision(self):
        return self.f.base.workspace.apply_table_change(self.token, **self.scope,
            expected_project_version=self.project_version, expected_revision=self.revision,
            shot_no="2", shot_id=self.shots[1]["shot_id"], field="visual",
            value="外场入口人群在展板旁握手交流，当前稿已变化", idempotency_key="advance-scope-revision")

    def test_two_selected_shots_are_the_only_search_and_semantic_targets(self):
        with patch.object(formal_default_images, "search_package_candidates",
                          wraps=formal_default_images.search_package_candidates) as search:
            proposal, parameters = self.propose()
        self.assertEqual([str(call.kwargs["slot"]["镜号"]) for call in search.call_args_list], ["2", "6"])
        self.assertEqual(self.semantic_targets, ["2", "6"])
        self.assertEqual(parameters["allowed_shot_ids"], [row["shot_id"] for row in self.shots])
        self.assertEqual(parameters["pending_shot_count"], len(self.shots) - 2)
        self.assertEqual(proposal["response"]["parameters"]["pending_shot_count"], len(self.shots) - 2)
        for selection in parameters["selections"]:
            original = self.candidates[selection["shot_no"]]
            self.assertEqual(selection["content_sha256"], original["content_sha256"])
            self.assertEqual(selection["sources"], original["sources"])
            self.assertTrue(selection["sources"])
        adopted = self.library.execute(self.token, project=self.project, parameters=parameters)
        self.assertEqual([(item["shot_no"], item["shot_id"]) for item in adopted["adopted_selections"]],
                         [(number, self.shots[int(number) - 1]["shot_id"]) for number in self.selected_nos])
        self.assertEqual(self.semantic_targets, ["2", "6"])
        self.assertEqual(self.f.workspace.read_progress(self.token, **self.scope)["selections"], [])

    def test_foreign_allowed_or_selected_targets_fail_before_matching(self):
        variants = [
            {"allowed_shot_ids": [*self.request()["allowed_shot_ids"], "shot_from_another_project"]},
            {"selections": [{"shot_no": "999", "candidate_key": self.candidates["2"]["candidate_key"]}]},
            {"allowed_shot_ids": [self.shots[1]["shot_id"]]},
        ]
        for index, mutation in enumerate(variants):
            with self.subTest(mutation=mutation), patch.object(formal_default_images, "search_package_candidates",
                    wraps=formal_default_images.search_package_candidates) as search:
                request = {**self.request("foreign-" + str(index)), **mutation}
                with self.assertRaises(WorkspaceServiceError) as raised:
                    self.library.propose(self.token, **request)
                self.assertIn(raised.exception.code, {"IMAGE_SCOPE_MISMATCH", "IMAGE_DECISION_MISMATCH"})
                search.assert_not_called()
        self.assertEqual(self.semantic_targets, [])
        self.assertEqual(self.adoption_tasks(), 0)

    def test_stale_working_revision_rejects_before_matching(self):
        self.advance_working_revision()
        with patch.object(formal_default_images, "search_package_candidates",
                          wraps=formal_default_images.search_package_candidates) as search:
            with self.assertRaisesRegex(WorkspaceServiceError, "DRAFT_REVISION_CONFLICT"):
                self.library.propose(self.token, **self.request())
            search.assert_not_called()
        self.assertEqual(self.semantic_targets, [])
        self.assertEqual(self.adoption_tasks(), 0)

    def test_scope_limited_proposal_still_rejects_later_revision_at_execution(self):
        _proposal, parameters = self.propose()
        self.advance_working_revision()
        with self.assertRaisesRegex(WorkspaceServiceError, "DRAFT_REVISION_CONFLICT"):
            self.library.execute(self.token, project=self.project, parameters=parameters)
        self.assertEqual(self.f.workspace.read_progress(self.token, **self.scope)["selections"], [])

    def test_scope_limited_proposal_keeps_content_sha_and_source_checks(self):
        _proposal, parameters = self.propose()
        for field in ("content_sha256", "sources"):
            with self.subTest(field=field):
                changed = deepcopy(parameters)
                if field == "content_sha256":
                    changed["selections"][0][field] = "0" * 64
                else:
                    changed["selections"][0][field][0]["row"] += 1
                with self.assertRaisesRegex(WorkspaceServiceError, "ASSET_PACKAGE_DRIFT"):
                    self.library.execute(self.token, project=self.project, parameters=changed)
        self.assertEqual(self.f.workspace.read_progress(self.token, **self.scope)["selections"], [])


if __name__ == "__main__":
    unittest.main()
