from __future__ import annotations

from dataclasses import replace
import json
import unittest

from mvp_v2.formal_image_workspace import FormalImageWorkspaceService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_image_workspace as image_fixture
from tests.test_mvp_v2_library_browse import build_anonymous_library_package


class ImageProgressTests(unittest.TestCase):
    def setUp(self):
        self.fixture = image_fixture.FormalImageWorkspaceTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.workspace
        self.scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.fixture.project_id}
        self.token = self.fixture.token
        self.current = self.service.read_current(self.token, **self.scope)

    def patch(self, selections, *, revision=0, key="progress-first", script_revision=0):
        return self.service.save_progress(
            self.token, **self.scope, base_version_id=self.current["base_version_id"],
            expected_project_version=self.current["project_version"], expected_revision=revision,
            expected_script_revision=script_revision, selections=selections, idempotency_key=key,
        )

    def selection(self, index=0, *, checked=True):
        shot = self.current["shots"][index]
        return {"shot_id": shot["shot_id"], "image_key": shot["selected_image_key"],
                "checked": checked, "decision_kind": "image_confirmed", "reuse_reason": "",
                "temporary_reason": "", "query": ""}

    def test_reused_image_retains_added_package_sources_and_original_analysis_evidence(self):
        from copy import deepcopy
        from tests.test_mvp_v2_library_browse import library_confirmation_agent
        f = self.fixture
        package = build_anonymous_library_package(f.base.generated.root / 'provenance-gallery')
        library = image_fixture.FormalDefaultImageService(projects=f.projects, tasks=image_fixture.TaskService(f.store),
            delivery=f.delivery, artifacts=f.resolver, image_pool=self.service.services.images,
            image_workspace=self.service, asset_package_resolver=lambda _: package, writes_enabled=True)
        project, context, plan, _, _ = library._context(self.token, self.scope['workspace_id'],
            self.scope['project_id'], self.current['project_version'])
        _, media = library._package_media(project, plan.brief)
        candidate = next(item for item in media if item['source_links'])
        agent = library_confirmation_agent(root=f.base.generated.root, store=f.store, projects=f.projects, service=library)
        session = library.tasks.create_agent_session(self.token, project_id=self.scope['project_id'])
        proposal = library.propose(self.token, **self.scope, agent_session_id=session['agent_session_id'],
            expected_project_version=self.current['project_version'],
            selections=[{'shot_no':'1', 'candidate_key':candidate['media_id']}], idempotency_key='multi-source-adopt')
        result = agent.confirm_action(self.token, proposal['task_id'], proposal_digest=proposal['response']['proposal_digest'])
        self.assertEqual(result['task']['status'], 'completed')
        image_id = result['response']['adopted_selections'][0]['image_key']
        brief_id, snapshot_id = self.service._brief_binding(context, plan.confirmed_brief_input)
        pool = self.service.services.images
        args = dict(**self.scope, brief_version_id=brief_id, brief_snapshot_id=snapshot_id,
            expected_project_lock_version=self.current['project_version'])
        before = next(image for image in pool.resolve_for_planning(self.token, **args).images if image.image_id == image_id)
        extra = deepcopy(candidate)
        extra['package_sources'].append({**deepcopy(candidate['package_sources'][0]), 'package_id':'anonymous-second-library',
            'sources':[{'file':'匿名追加图库.xlsx','sheet':'创意库','row':19}]})
        pool.adopt_package_images(self.token, **args, package_id=extra['package_sources'][0]['package_id'],
            package_version=extra['package_sources'][0]['package_version'], candidates=(extra,))
        after = next(image for image in pool.resolve_for_planning(self.token, **args).images if image.image_id == image_id)
        self.assertEqual(after.current_analysis.status, before.current_analysis.status)
        self.assertEqual(after.current_analysis.created_at, before.current_analysis.created_at)
        payload = pool.artifacts.read_analysis(after.current_analysis.evidence)
        self.assertEqual(len(payload['package_sources']), 2)
        self.assertTrue(any(link.source_label == '匿名追加图库.xlsx' and link.row == 19 for link in after.source_links))
        self.assertEqual(self.service.read_progress(self.token, **self.scope)['selections'], [])

    def test_project_and_library_recommendations_follow_saved_working_draft_without_version_save(self):
        f = self.fixture
        package = build_anonymous_library_package(f.base.generated.root / 'draft-gallery')
        library = image_fixture.FormalDefaultImageService(projects=f.projects, tasks=image_fixture.TaskService(f.store),
            delivery=f.delivery, artifacts=f.resolver, image_pool=self.service.services.images,
            image_workspace=self.service, asset_package_resolver=lambda _: package, writes_enabled=True)
        before = library.candidates(self.token, **self.scope, expected_project_version=self.current['project_version'], shot_no='3')
        script = f.base.workspace.read_current(self.token, **self.scope)
        revised = f.base.workspace.apply_table_change(self.token, **self.scope,
            expected_project_version=self.current['project_version'], expected_revision=0, shot_no='3',
            shot_id=script['shots'][2]['shot_id'], field='visual', value='外场入口人群在展板前交流',
            idempotency_key='recommend-working-draft')
        updated = library.candidates(self.token, **self.scope, expected_project_version=self.current['project_version'], shot_no='3')
        project = self.service.search_shot(self.token, **self.scope, expected_project_version=self.current['project_version'],
            base_version_id=self.current['base_version_id'], shot_no='3', query='')
        self.assertEqual(updated['shots'][0]['visual'], '外场入口人群在展板前交流')
        self.assertNotEqual(before['shots'][0]['visual'], updated['shots'][0]['visual'])
        self.assertEqual(project['shot_context']['visual'], updated['shots'][0]['visual'])
        self.assertEqual(updated['script_revision'], revised['revision'])
        self.assertEqual(project['script_revision'], revised['revision'])
        self.assertEqual(updated['shots'][0]['shot_id'], script['shots'][2]['shot_id'])
        self.assertEqual(self.service.read_progress(self.token, **self.scope)['base_version_id'], self.current['base_version_id'])

    def test_incomplete_progress_survives_restart_without_version_or_review_writes(self):
        before = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
        chosen = [self.selection(index) for index in (0, 2)]
        result = self.patch(chosen)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["selected_count"], 2)
        self.assertEqual(result["pending_count"], self.current["shot_count"] - 2)
        self.assertFalse(result["review_eligible"])
        restarted = FormalImageWorkspaceService(
            replace(self.service.services), runs_root=self.fixture.base.generated.root / "runs",
            source_root=self.fixture.base.generated.source_root,
        )
        restored = restarted.read_progress(self.token, **self.scope)
        self.assertEqual(restored["selections"], result["selections"])
        self.assertEqual(restored["saved_at"], result["saved_at"])
        self.assertEqual(self.fixture.projects.list_project_versions(self.token, self.fixture.project_id), before)
        self.assertFalse(self.fixture.base.workspace.read_current(self.token, **self.scope)["can_save_version"])
        self.assertFalse(any(shot["confirmed"] for shot in restarted.read_current(self.token, **self.scope)["shots"]))
        for secret in ("actor_id", "analysis_ref", "artifact://", str(self.fixture.base.generated.root)):
            self.assertNotIn(secret, json.dumps(restored))

    def test_http_progress_requires_auth_and_scope_and_keeps_batch_atomic(self):
        fixture = self.fixture
        agent = image_fixture.ReadonlyAgentService(image_fixture.ReadonlyAgentServices(
            store=fixture.store, identity=image_fixture.LocalIdentityAdapter(fixture.store),
            projects=fixture.projects, tasks=image_fixture.TaskService(fixture.store),
            versions=image_fixture.VersionService(fixture.store), artifacts=fixture.resolver,
        ))
        client = image_fixture.TestClient(image_fixture.create_app(agent_api=agent, formal_image_service=self.service))
        self.addCleanup(client.close)
        path = f"/api/formal/projects/{fixture.project_id}/image-workspace/progress"
        query = {"workspace_id": fixture.workspace_id}
        headers = {"X-MPlus-Agent-Auth": self.token, "X-MPlus-UI-Token": client.app.state.session_token}
        self.assertEqual(client.get(path, params=query).status_code, 401)
        self.assertEqual(client.get(path, params={"workspace_id": "workspace_other"}, headers=headers).status_code, 403)
        body = {"base_version_id": self.current["base_version_id"], "expected_project_version": self.current["project_version"],
                "expected_revision": 0, "expected_script_revision": 0, "selections": [self.selection()], "idempotency_key": "http-progress"}
        self.assertEqual(client.patch(path, params=query, json=body).status_code, 401)
        saved = client.patch(path, params=query, json=body, headers=headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["selected_count"], 1)
        self.assertEqual(client.patch(path, params=query, json=body, headers=headers).json(), saved.json())
        invalid = {**body, "expected_revision": 1, "idempotency_key": "http-invalid", "selections": [self.selection(1), {**self.selection(2), "shot_id": "foreign-shot"}]}
        self.assertEqual(client.patch(path, params=query, json=invalid, headers=headers).status_code, 409)
        self.assertEqual(client.get(path, params=query, headers=headers).json()["revision"], 1)
        self.assertEqual(client.patch(path, params=query, json={**body, "unexpected": True}, headers=headers).status_code, 422)

    def test_idempotent_retry_conflict_and_invalid_batch_cannot_overwrite_progress(self):
        first = self.patch([self.selection()])
        second = self.patch([self.selection(1)], revision=1, key="progress-second")
        replay = self.patch([self.selection()])
        self.assertEqual(replay["revision"], first["revision"])
        self.assertEqual(self.service.read_progress(self.token, **self.scope)["revision"], second["revision"])
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_PROGRESS_CONFLICT"):
            self.patch([self.selection(2)], revision=1, key="stale")
        with self.assertRaisesRegex(WorkspaceServiceError, "IDEMPOTENCY_CONFLICT"):
            self.patch([self.selection(2)])
        invalid = {**self.selection(2), "shot_id": "other-project-shot"}
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_SCOPE_MISMATCH"):
            self.patch([self.selection(2), invalid], revision=2, key="invalid-batch")
        self.assertEqual(self.service.read_progress(self.token, **self.scope)["selections"], second["selections"])
        noop = self.patch([self.selection(1)], revision=2, key="no-change")
        self.assertEqual(noop["revision"], 2)

    def test_progress_allows_unfinished_reasons_and_dirty_text_without_marking_images_confirmed(self):
        script = self.fixture.base.workspace.read_current(self.token, **self.scope)
        changed = self.fixture.base.workspace.apply_table_change(
            self.token, **self.scope, expected_project_version=self.current["project_version"],
            expected_revision=0, shot_no="1", shot_id=script["shots"][0]["shot_id"],
            field="sound", value="保存中的匿名声音", idempotency_key="progress-sound",
        )
        pending = {**self.selection(1, checked=False), "image_key": "", "decision_kind": "temporary_text_basis"}
        result = self.patch([self.selection(), pending], script_revision=changed["revision"])
        self.assertEqual(len(result["selections"]), 2)
        self.assertFalse(result["review_eligible"])
        self.assertEqual(self.fixture.base.workspace.read_current(self.token, **self.scope)["revision"], changed["revision"])
        with self.assertRaisesRegex(WorkspaceServiceError, "DRAFT_REVISION_CONFLICT"):
            self.patch([self.selection(2)], revision=1, key="old-script")

    def test_later_page_library_reference_survives_restart_without_adoption_and_detects_blob_drift(self):
        fixture = self.fixture
        package = build_anonymous_library_package(fixture.base.generated.root / "progress-gallery")
        library = image_fixture.FormalDefaultImageService(
            projects=fixture.projects, tasks=image_fixture.TaskService(fixture.store),
            delivery=fixture.delivery, artifacts=fixture.resolver, image_pool=self.service.services.images,
            image_workspace=self.service, asset_package_resolver=lambda _: package, writes_enabled=True,
        )
        candidates = library.candidates(self.token, **self.scope, expected_project_version=self.current["project_version"], view="library", page=3, page_size=12)
        self.assertGreater(candidates["total_groups"], 24)
        selected = candidates["library_candidates"][0]
        saved = self.service.save_progress(self.token, **self.scope,
            base_version_id=self.current["base_version_id"], expected_project_version=self.current["project_version"],
            expected_revision=0, expected_script_revision=0, idempotency_key="later-page-progress",
            selections=[{"shot_id": self.current["shots"][2]["shot_id"], "library_candidate_key": selected["candidate_key"], "checked": True}],
            library_service=library)
        restarted = FormalImageWorkspaceService(replace(self.service.services), runs_root=fixture.base.generated.root / "runs", source_root=fixture.base.generated.source_root)
        restored = restarted.read_progress(self.token, **self.scope, library_service=library)
        self.assertEqual(restored, saved)
        self.assertEqual(restored["selected_count"], 1)
        self.assertEqual(restored["selections"][0]["candidate"]["content_sha256"], selected["content_sha256"])
        self.assertFalse(restored["review_eligible"])
        with fixture.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_image_package_source_links l JOIN project_images i ON i.project_image_id=l.project_image_id WHERE i.project_id=?", (fixture.project_id,)).fetchone()[0], 0)
        _, media = library._package_media({"category_id": "launch"}, {"脚本类型": "发布会"})
        target = image_fixture.Path(next(item["path"] for item in media if item["media_id"] == selected["candidate_key"]))
        target.write_bytes(target.read_bytes() + b"anonymous-drift")
        drifted = restarted.read_progress(self.token, **self.scope, library_service=library)
        self.assertTrue(drifted["selections"][0]["unavailable"])
        self.assertFalse(drifted["selections"][0]["checked"])

    def test_script_child_preserves_saved_choices_and_reopens_only_changed_picture_basis(self):
        self.patch([self.selection(0), self.selection(1)])
        script = self.fixture.base.workspace.read_current(self.token, **self.scope)
        changed = self.fixture.base.workspace.apply_table_change(
            self.token, **self.scope, expected_project_version=self.current["project_version"],
            expected_revision=0, shot_no="1", shot_id=script["shots"][0]["shot_id"],
            field="sound", value="匿名环境声与音乐", idempotency_key="inherit-progress-sound",
        )
        versions_before = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_CONFIRMATION_REQUIRED"):
            self.fixture.base.workspace.save_version(
                self.token, **self.scope, expected_project_version=self.current["project_version"],
                expected_revision=changed["revision"], reason="等待全稿配图", idempotency_key="inherit-progress-incomplete",
            )
        after = self.service.read_progress(self.token, **self.scope)
        self.assertEqual(len(after["selections"]), 2)
        self.assertTrue(all(item["checked"] for item in after["selections"]))
        self.assertEqual(after["base_version_id"], self.current["base_version_id"])
        self.assertEqual(self.fixture.projects.list_project_versions(self.token, self.fixture.project_id), versions_before)
        latest = self.fixture.base.workspace.read_current(self.token, **self.scope)
        visual = self.fixture.base.workspace.apply_table_change(
            self.token, **self.scope, expected_project_version=latest["project_version"],
            expected_revision=latest["revision"], shot_no="2", shot_id=latest["shots"][1]["shot_id"],
            field="visual", value=latest["shots"][1]["visual"] + "，镜头缓慢掠过现场花艺细节。",
            idempotency_key="inherit-progress-visual",
        )
        pending = self.service.read_progress(self.token, **self.scope)
        by_shot = {item["shot_no"]: item for item in pending["selections"]}
        self.assertTrue(by_shot["1"]["checked"])
        self.assertFalse(by_shot["2"]["checked"])
        self.assertTrue(by_shot["2"]["needs_recheck"])
        self.service.save_progress(
            self.token, **self.scope, base_version_id=self.current["base_version_id"],
            expected_project_version=self.current["project_version"], expected_revision=pending["revision"],
            expected_script_revision=visual["revision"],
            selections=[self.selection(i) for i in range(len(self.current["shots"]))],
            idempotency_key="inherit-progress-complete-images",
        )
        child = self.fixture.base.workspace.save_version(
            self.token, **self.scope, expected_project_version=self.current["project_version"],
            expected_revision=visual["revision"], reason="声音画面配图统一保存", idempotency_key="inherit-progress-child",
        )
        self.assertNotEqual(child["base_version_id"], self.current["base_version_id"])
        self.assertEqual(len(self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)), len(versions_before) + 1)
        final = self.service.read_current(self.token, **self.scope)
        self.assertTrue(all(item["confirmed"] for item in final["shots"]))
        self.assertEqual([item["selected_image_key"] for item in final["shots"]],
                         [item["selected_image_key"] for item in self.current["shots"]])

    def test_partial_image_commit_clears_only_submitted_progress_and_keeps_parent(self):
        saved = self.service.save_version(self.token, **self.scope,
            expected_project_version=self.current["project_version"],
            selections=tuple(image_fixture.ImageShotSelection(shot["shot_no"], shot["selected_image_key"], True) for shot in self.current["shots"]),
            idempotency_key="progress-initial-images")
        self.current = saved
        first, second = self.selection(0), self.selection(1)
        first["image_key"] = next(item["image_key"] for item in saved["shots"][0]["candidates"] if item["image_key"] != first["image_key"])
        first["reuse_reason"] = "当前镜头有理由复用已核对画面"
        self.patch([first, second])
        proposal = self.service.preview_version(self.token, **self.scope,
            base_version_id=saved["base_version_id"], expected_project_version=saved["project_version"],
            allowed_shot_ids=[first["shot_id"]],
            selections=(image_fixture.ImageShotSelection(saved["shots"][0]["shot_no"], first["image_key"], True, reuse_reason=first["reuse_reason"], candidate_snapshot_digest=saved["shots"][0]["candidate_snapshot_digest"]),),
            idempotency_key="progress-partial-images")
        self.service.confirm_version(self.token, proposal_id=proposal["proposal_id"], proposal_digest=proposal["proposal_digest"])
        child = self.service.read_progress(self.token, **self.scope)
        self.assertEqual([item["shot_id"] for item in child["selections"]], [second["shot_id"]])
        with self.fixture.store.connection() as connection:
            row = connection.execute("SELECT selections_json FROM formal_image_progress WHERE project_id=? AND base_version_id=?", (self.fixture.project_id, saved["base_version_id"])).fetchone()
        self.assertEqual(set(json.loads(row[0])), {first["shot_id"], second["shot_id"]})
