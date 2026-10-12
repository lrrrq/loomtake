"""Real-service regressions for lawful human approval inheritance across versions."""
from __future__ import annotations

import hashlib
import json
import socket
from threading import Thread
import time
import unittest
from urllib.parse import quote

from playwright.sync_api import expect, sync_playwright
import uvicorn

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
from mvp_v2.formal_image_workspace import ImageShotSelection
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import create_app
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore
from tests import test_mvp_v2_formal_review_workspace as review_fixture


class ReviewInheritanceTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        fixture = review_fixture.FormalReviewWorkspaceTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.review = fixture.review
        self.store = fixture.store
        self.root = fixture.root
        self.token = fixture.token
        self.workspace_id = fixture.workspace_id
        self.project_id = fixture.project_id
        self.script = fixture.base.base.workspace
        self.images = fixture.base.workspace
        self.parent = fixture._confirm_current()
        self.parent_id = self.parent["base_version_id"]
        self.original = self._image_decisions(self.parent_id)
        self.assertGreaterEqual(len(self.original), 4)
        self.original_rows = self._version_records(self.parent_id)
        self.original_artifacts = {
            item["evidence_ref"]: self._evidence_path(item["evidence_sha256"]).read_bytes()
            for item in self.original.values()
        }
        identity = LocalIdentityAdapter(self.store)
        workspace = ProjectService(self.store).get_workspace(self.token, self.workspace_id)
        reviewer = identity.create_actor(
            self.token, workspace_id=self.workspace_id,
            display_name="匿名接手审核员", auth_source="local", role="reviewer",
            expected_workspace_version=workspace["workspace_version"],
        )
        self.other_actor_id = reviewer["actor_id"]
        self.other_token = identity.issue_auth_session(
            actor_id=self.other_actor_id, token="review-inheritance-other-session",
        )["auth_token"]
        self.assertNotIn(self.other_actor_id, {item["reviewer_actor_id"] for item in self.original.values()})

    def _evidence_path(self, digest):
        return self.root / "review-evidence" / "artifacts" / "version-review-evidence" / digest[:2] / f"{digest}.json"

    def _image_decisions(self, version_id):
        with self.store.connection() as connection:
            rows = connection.execute(
                """SELECT i.shot_no, d.* FROM version_review_items i
                JOIN version_review_human_decisions d ON d.review_item_id=i.review_item_id
                WHERE i.version_id=? AND i.kind='image'
                ORDER BY d.decided_at, d.decision_id""", (version_id,),
            ).fetchall()
        return {str(item["shot_no"]): dict(item) for item in rows}

    def _version_records(self, version_id):
        with self.store.connection() as connection:
            return {
                table: [tuple(row) for row in connection.execute(
                    f"SELECT * FROM {table} WHERE version_id=? ORDER BY 1", (version_id,),
                )]
                for table in ("version_review_items", "version_review_human_decisions", "version_quality_final_confirmations")
            }

    def _save_script_change(self, field="sound"):
        current = self.script.read_current(self.token, workspace_id=self.workspace_id, project_id=self.project_id)
        target = next(item for item in current["shots"] if item["shot_no"] == "3")
        value = "保留持续底乐，第三镜加入轻微现场交流声并平稳衔接前后镜。" if field == "sound" else target["visual"] + "；主体保持原位，前景增添真实现场层次。"
        changed = self.script.apply_table_change(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=current["project_version"], expected_revision=current["revision"],
            shot_no="3", shot_id=target["shot_id"], field=field, value=value,
            idempotency_key=f"inheritance-{field}-edit",
        )
        return self.script.save_version(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=current["project_version"], expected_revision=changed["revision"],
            reason="仅更新第3镜" + ("声音" if field == "sound" else "画面"),
            idempotency_key=f"inheritance-{field}-save",
        )

    def _start_child(self):
        current = self.review.read_current(self.other_token, workspace_id=self.workspace_id, project_id=self.project_id)
        self.assertNotEqual(current["base_version_id"], self.parent_id)
        return self.review.start_review(
            self.other_token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=current["project_version"], idempotency_key="inheritance-start-child",
        )

    def _assert_inherited_images(self, child, excluded=()):
        issues = {item["shot_no"]: item for item in child["issues"] if item["kind"] == "image"}
        self.assertEqual(set(issues), set(self.original))
        records = self._image_decisions(child["base_version_id"])
        for shot_no, original in self.original.items():
            if shot_no in excluded:
                self.assertEqual(issues[shot_no]["status"], "open")
                self.assertFalse(issues[shot_no].get("inherited_review"))
                self.assertNotIn(shot_no, records)
                continue
            self.assertEqual(issues[shot_no]["status"], "approve", f"第{shot_no}镜未变图片质量批准应沿用")
            source = issues[shot_no].get("inherited_review")
            self.assertIsInstance(source, dict)
            self.assertEqual(source["version_label"], self.parent["version_label"])
            self.assertTrue(source["reviewer"])
            self.assertEqual(source["reviewed_at"], original["decided_at"])
            self.assertEqual(records[shot_no]["reviewer_actor_id"], original["reviewer_actor_id"])
            self.assertEqual(records[shot_no]["decided_at"], original["decided_at"])
            self.assertEqual(records[shot_no]["decision"], "approve")
            self.assertNotEqual(records[shot_no]["evidence_ref"], original["evidence_ref"])
            inherited_artifact = json.loads(self._evidence_path(records[shot_no]["evidence_sha256"]).read_text())
            self.assertIn(original["evidence_ref"], json.dumps(inherited_artifact, ensure_ascii=False))
            self.assertIn(original["evidence_sha256"], json.dumps(inherited_artifact, ensure_ascii=False))
        self.assertIsNone(child["final_confirmation"])
        self.assertEqual(self._version_records(self.parent_id), self.original_rows)
        for item in self.original.values():
            self.assertEqual(self._evidence_path(item["evidence_sha256"]).read_bytes(), self.original_artifacts[item["evidence_ref"]])

    def test_sound_child_inherits_original_image_reviewer_and_time_but_reopens_sound_edges_and_final(self):
        self._save_script_change()
        child = self._start_child()
        self._assert_inherited_images(child)
        changes = [item for item in child["issues"] if item["kind"] == "change"]
        self.assertEqual([item["shot_no"] for item in changes], ["3"])
        self.assertEqual(changes[0]["status"], "open")
        self.assertEqual(set(changes[0]["affected_adjacent_edges"]), {"2->3", "3->4"})
        self.assertIn("声音", changes[0]["change_summary"])
        self.assertFalse(child["ready_for_final"])
        self._assert_real_browser_shows_inherited_reviews(child)
        with self.assertRaises(WorkspaceServiceError):
            self.review.confirm_final(
                self.other_token, workspace_id=self.workspace_id, project_id=self.project_id,
                expected_project_version=child["project_version"], note="尚未复核声音不得确认",
                idempotency_key="inheritance-premature-final",
            )
        reopened = self.fixture._review_service(WorkspaceStore(self.store.database_path)).read_current(
            self.other_token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self._assert_inherited_images(reopened)
        approved = self.review.decide(
            self.other_token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=child["project_version"], issue_keys=tuple(item["issue_key"] for item in changes),
            decision="approve", note="已听读第3镜声音并核对2至3、3至4的承接。", idempotency_key="inheritance-real-sound-review",
        )
        final = self.review.confirm_final(
            self.other_token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=approved["project_version"], note="确认本次声音子版整片。",
            idempotency_key="inheritance-new-final",
        )
        self.assertEqual(final["state"], "confirmed")
        self.assertEqual(self._version_records(self.parent_id), self.original_rows)
        with self.store.connection() as connection:
            finals = connection.execute("SELECT version_id, reviewer_actor_id FROM version_quality_final_confirmations").fetchall()
        self.assertEqual({row["version_id"] for row in finals}, {self.parent_id, child["base_version_id"]})
        self.assertEqual(next(row["reviewer_actor_id"] for row in finals if row["version_id"] != self.parent_id), self.other_actor_id)

    def _assert_real_browser_shows_inherited_reviews(self, child):
        agent = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store, identity=LocalIdentityAdapter(self.store),
            projects=ProjectService(self.store), tasks=TaskService(self.store),
            versions=VersionService(self.store), artifacts=RunArtifactResolver(self.root / "runs"),
        ))
        adapter = FormalBriefInputAdapter(self.fixture.base.base.generated.briefs, data_root=self.root / "runtime")
        app = create_app(
            agent_api=agent, formal_brief_adapter=adapter,
            formal_brief_upload_factory=lambda filename, stream, critical: UploadedDocument(filename, stream, critical),
            formal_script_service=self.script, formal_image_service=self.images, formal_review_service=self.review,
        )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(server.started, "匿名真实服务未启动")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.set_default_timeout(15_000)
                    page.add_init_script(
                        "localStorage.setItem('mplus.agent.auth', " + json.dumps(self.other_token) + ");"
                        "localStorage.setItem('mplus.agent.workspace', " + json.dumps(self.workspace_id) + ");"
                    )
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"{base}/projects/{quote(self.project_id)}/review")
                    page.wait_for_function(
                        "version => state.formalReview?.base_version_id === version && !state.formalReviewBusy",
                        arg=child["base_version_id"], timeout=30_000,
                    )
                    others = page.locator(".ui00-review-others")
                    expect(others).to_be_visible()
                    others.locator("summary").first.click()
                    notes = page.locator("[data-review-inherited]")
                    expect(notes).to_have_count(len(self.original))
                    expected = next(item["inherited_review"] for item in child["issues"] if item["kind"] == "image")
                    expect(notes.first).to_be_visible()
                    expect(notes.first).to_contain_text("沿用" + expected["version_label"])
                    expect(notes.first).to_contain_text(expected["reviewer"])
                    expect(notes.first.locator("time")).to_have_attribute("datetime", expected["reviewed_at"])
                    expect(notes.first.locator("time")).not_to_contain_text("待确认")
                    expect(notes.first).not_to_contain_text("匿名接手审核员")
                    self.assertEqual(page.locator(".ui00-review-item[data-status='approve'] [data-review-decision]").count(), 0)
                    expect(page.locator("[data-review-group='focused']")).to_contain_text("声音")
                    for width in (1440, 375):
                        page.set_viewport_size({"width": width, "height": 1000})
                        expect(notes.first).to_be_visible()
                        self.assertLessEqual(page.locator("html").evaluate("el => el.scrollWidth"), width + 1)
                    self.assertEqual(errors, [])
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(10)
            listener.close()

    def _replace_or_reconfirm_third_image(self, *, replace_image):
        current = self.images.read_current(self.token, workspace_id=self.workspace_id, project_id=self.project_id)
        target = next(item for item in current["shots"] if item["shot_no"] == "3")
        searched = self.images.search_shot(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            shot_no="3", base_version_id=current["base_version_id"],
            expected_project_version=current["project_version"], query="",
        )
        image_key = target["selected_image_key"]
        if replace_image:
            image_key = next(item["image_key"] for item in searched["candidates"] if item["image_key"] != image_key)
        proposal = self.images.preview_version(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=current["project_version"], base_version_id=current["base_version_id"],
            selections=(ImageShotSelection("3", image_key, True,
                candidate_snapshot_digest=searched["candidate_snapshot_digest"],
                reuse_reason="本次明确核对第三镜的图片使用"),),
            allowed_shot_ids=[target["shot_id"]], idempotency_key="inheritance-third-image-save",
        )
        self.assertEqual(proposal["affected_shots"], ["3"])
        return self.images.confirm_version(
            self.token, proposal_id=proposal["proposal_id"], proposal_digest=proposal["proposal_digest"],
        )

    def test_replaced_third_image_reopens_only_its_image_quality(self):
        self._replace_or_reconfirm_third_image(replace_image=True)
        self._assert_inherited_images(self._start_child(), excluded={"3"})

    def test_visual_change_through_reconfirmed_image_cannot_reuse_third_quality_approval(self):
        scope = dict(workspace_id=self.workspace_id, project_id=self.project_id)
        current = self.script.read_current(self.token, **scope)
        target = next(item for item in current["shots"] if item["shot_no"] == "3")
        before_versions = self.fixture.base.projects.list_project_versions(self.token, self.project_id)
        changed = self.script.apply_table_change(
            self.token, **scope, expected_project_version=current["project_version"],
            expected_revision=current["revision"], shot_no="3", shot_id=target["shot_id"], field="visual",
            value=target["visual"] + "；主体保持原位，前景增添真实现场层次。",
            idempotency_key="inheritance-visual-edit",
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_CONFIRMATION_REQUIRED"):
            self.script.save_version(self.token, **scope, expected_project_version=current["project_version"],
                expected_revision=changed["revision"], reason="等待配图重新核对", idempotency_key="inheritance-visual-incomplete")
        self.assertEqual(self.fixture.base.projects.list_project_versions(self.token, self.project_id), before_versions)
        image = self.images.read_current(self.token, **scope)
        image_key = next(item["selected_image_key"] for item in image["shots"] if item["shot_no"] == "3")
        searched = self.images.search_shot(self.token, **scope, expected_project_version=current["project_version"],
            base_version_id=current["base_version_id"], shot_no="3", query="")
        self.assertIn(image_key, {item["image_key"] for item in searched["candidates"]})
        progress = self.images.read_progress(self.token, **scope)
        self.images.save_progress(self.token, **scope, base_version_id=current["base_version_id"],
            expected_project_version=current["project_version"], expected_revision=progress["revision"],
            expected_script_revision=changed["revision"],
            selections=[dict(shot_id=target["shot_id"], image_key=image_key, checked=True,
                             reuse_reason="本次按新画面明确核对第三镜的图片使用")],
            idempotency_key="inheritance-visual-reconfirm")
        self.script.save_version(self.token, **scope, expected_project_version=current["project_version"],
            expected_revision=changed["revision"], reason="画面配图统一保存", idempotency_key="inheritance-visual-save")
        self.assertEqual(len(self.fixture.base.projects.list_project_versions(self.token, self.project_id)), len(before_versions) + 1)
        self._assert_inherited_images(self._start_child(), excluded={"3"})

    def _assert_corruption_does_not_approve(self):
        child_id = ProjectService(self.store).get_project(self.token, self.project_id)["current_version_id"]
        try:
            child = self._start_child()
        except WorkspaceServiceError:
            pass
        else:
            self.assertNotEqual(next(item["status"] for item in child["issues"] if item["kind"] == "image" and item["shot_no"] == "3"), "approve")
        self.assertNotEqual(self._image_decisions(child_id).get("3", {}).get("decision"), "approve")
        self.assertEqual(self._version_records(child_id)["version_quality_final_confirmations"], [])

    def test_missing_original_approval_artifact_cannot_be_inherited(self):
        self._save_script_change()
        self._evidence_path(self.original["3"]["evidence_sha256"]).unlink()
        self._assert_corruption_does_not_approve()

    def test_tampered_original_approval_artifact_cannot_be_inherited(self):
        self._save_script_change()
        path = self._evidence_path(self.original["3"]["evidence_sha256"])
        path.write_bytes(path.read_bytes().replace(b'"decision":"approve"', b'"decision":"return"'))
        self.assertNotEqual(hashlib.sha256(path.read_bytes()).hexdigest(), self.original["3"]["evidence_sha256"])
        self._assert_corruption_does_not_approve()

    def test_foreign_project_ancestor_cannot_supply_approval(self):
        self._save_script_change()
        projects = ProjectService(self.store)
        workspace = projects.get_workspace(self.token, self.workspace_id)
        other = projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名无关项目", category_id="launch",
            expected_workspace_version=workspace["workspace_version"],
        )
        # Deliberately corrupt only this disposable fixture to exercise lineage
        # validation beneath the normal immutable-version creation boundary.
        with self.store.transaction() as connection:
            trigger = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='project_versions_no_update'").fetchone()[0]
            connection.execute("DROP TRIGGER project_versions_no_update")
            connection.execute("UPDATE project_versions SET project_id=? WHERE version_id=?", (other["project_id"], self.parent_id))
            connection.execute(trigger)
        self._assert_corruption_does_not_approve()


if __name__ == "__main__":
    unittest.main()
