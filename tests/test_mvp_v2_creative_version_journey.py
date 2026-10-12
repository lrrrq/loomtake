"""Real multi-version acceptance for staged sound and original shooting edits.

The only generated content is an anonymous provider fixture. All proposals,
drafts, versions, image decisions, review records and exports use real cores.
HTTP DTO checks complement, and do not claim to replace, browser acceptance.
"""
from __future__ import annotations

from dataclasses import replace
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pypdf import PdfReader

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.excel_io import ADJACENT_REVIEW_SHEET_NAME
from mvp_v2.executable_agent import ExecutableAgentServices
from mvp_v2.formal_agent import FormalExecutableAgentService, FormalExecutionBackend
from mvp_v2.formal_image_workspace import FormalImageWorkspaceService
from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
from mvp_v2.formal_script_revision import FormalScriptRevisionService
from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import create_app
from mvp_v2.project_image_authority import ProjectImagePoolService
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.run_contract import sha256_file
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceStore
from tests import test_mvp_v2_formal_generation_service as generation_fixture
from tests import test_mvp_v2_formal_review_workspace as review_fixture
from tests.test_mvp_v2_formal_browser_journey import _assert_review_workbook_matches_saved_version


class CreativeVersionJourneyTests(unittest.TestCase):
    maxDiff = None

    def _json(self, suffix: str) -> dict:
        response = self.client.get(self.api + suffix, headers=self.headers,
                                   params={"workspace_id": self.scope["workspace_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _stage(self, name: str, **values):
        self.evidence.update(values)
        self.evidence["completed_stages"].append(name)

    def _same_image_decision(self, before: dict, after: dict):
        # A child snapshot has its own row ID; human identity and timestamp,
        # image identity, complete source, analysis and reuse reason must survive.
        self.assertEqual({k: v for k, v in after.items() if k != "decision_id"},
                         {k: v for k, v in before.items() if k != "decision_id"})

    def _quality_image_decisions(self, version_id: str) -> dict:
        with self.store.connection() as connection:
            rows = connection.execute("""SELECT i.shot_no, d.* FROM version_review_items i
                JOIN version_review_human_decisions d ON d.review_item_id=i.review_item_id
                WHERE i.version_id=? AND i.kind='image' ORDER BY d.decided_at, d.decision_id""",
                (version_id,)).fetchall()
        return {str(row["shot_no"]): dict(row) for row in rows}

    def _quality_evidence_path(self, digest: str) -> Path:
        return self.root / "review-evidence" / "artifacts" / "version-review-evidence" / digest[:2] / f"{digest}.json"

    def _artifacts(self, *, include_decisions: bool = True) -> dict:
        current = self.script.read_current(self.token, **self.scope)
        context = self.images.services.delivery.resolve(
            self.token, self.scope["project_id"], current["base_version_id"])
        result = {"version_id": context.version_id, "run_id": context.run_id}
        for name in ("script_plan", "script_draft", "visual_binding"):
            path = self.images.services.artifacts.resolve_stage_artifact(context.run_id, name).path
            result[name] = json.loads(path.read_text(encoding="utf-8"))
            result[name + "_path"] = str(path)
            result[name + "_sha256"] = sha256_file(path)
        if include_decisions:
            decisions = self.images.services.artifacts.resolve_artifact(context.run_id, "image_decisions").path
            result["image_decisions"] = json.loads(decisions.read_text(encoding="utf-8"))
        return result

    def _make_client(self, revision=None):
        revision = revision or FormalScriptRevisionService(projects=ProjectService(self.store),
            tasks=TaskService(self.store), workspace=self.script, image_workspace=self.images, writes_enabled=True)
        readonly = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store, identity=LocalIdentityAdapter(self.store), projects=ProjectService(self.store),
            tasks=TaskService(self.store), versions=VersionService(self.store),
            artifacts=RunArtifactResolver(self.root / "runs")))
        agent = FormalExecutableAgentService(ExecutableAgentServices(
            readonly=readonly, projects=ProjectService(self.store), tasks=TaskService(self.store),
            confirmations=ActionConfirmationService(self.store, b"v" * 32),
            idempotency=ToolIdempotencyService(self.store), brief_store=BriefArtifactStore(self.root / "runtime"),
            backend=FormalExecutionBackend()), generation=self.base.base.base.generation,
            advisor=None, writes_enabled=True, script_revision=revision)
        client = TestClient(create_app(agent_api=agent, formal_script_service=self.script,
            formal_image_service=self.images, formal_review_service=self.review))
        self.addCleanup(client.close)
        return client, agent

    def _confirm_review(self, key: str) -> dict:
        current = self.review.read_current(self.token, **self.scope)
        self.assertEqual(current["state"], "ready", current)
        started = self.review.start_review(self.token, **self.scope,
            expected_project_version=current["project_version"], idempotency_key=key + "-start")
        keys = tuple(item["issue_key"] for item in started["issues"] if item["status"] != "approve")
        self.assertTrue(keys)
        approved = self.review.decide(self.token, **self.scope,
            expected_project_version=started["project_version"], issue_keys=keys,
            decision="approve", note="逐镜核对画面、声音、原创依据、配图来源及相邻承接。",
            idempotency_key=key + "-approve")
        self.assertEqual(approved["state"], "ready_final", approved)
        final = self.review.confirm_final(self.token, **self.scope,
            expected_project_version=approved["project_version"],
            note="本版本全部镜头、配图及变化范围已检查，明确完成整片确认。",
            idempotency_key=key + "-final")
        self.assertEqual(final["state"], "confirmed", final)
        return final

    def _change(self, current: dict, *, shot: dict, field: str, value: str, key: str) -> dict:
        return self.script.apply_table_change(self.token, **self.scope,
            expected_project_version=current["project_version"], expected_revision=current["revision"],
            shot_no=shot["shot_no"], shot_id=shot["shot_id"], field=field, value=value,
            idempotency_key=key)

    def _save(self, current: dict, reason: str, key: str) -> dict:
        values = {**self.scope, "expected_project_version": current["project_version"],
                  "expected_revision": current["revision"], "reason": reason, "idempotency_key": key}
        before = ProjectService(self.store).list_project_versions(self.token, self.scope["project_id"])
        saved = self.script.save_version(self.token, **values)
        self.assertEqual(saved["save_status"], "completed", saved)
        replayed = self.script.save_version(self.token, **values)
        self.assertEqual(saved, replayed)
        after = ProjectService(self.store).list_project_versions(self.token, self.scope["project_id"])
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(after[:-1], before)
        self.assertEqual(after[-1]["parent_version_id"], before[-1]["version_id"])
        return saved

    def test_confirmed_sound_draft_original_branch_and_formal_exports_survive_restart(self):
        evidence_root = os.environ.get("MPLUS_V3_EVIDENCE_ROOT", "").strip()
        base = review_fixture.FormalReviewWorkspaceTests("runTest")
        if evidence_root:
            parent = Path(evidence_root) / "creative-version-journey"
            parent.mkdir(parents=True, exist_ok=True)
            retained = Path(tempfile.mkdtemp(prefix="anonymous-", dir=parent))
            with patch.object(generation_fixture.tempfile, "TemporaryDirectory",
                              return_value=SimpleNamespace(name=str(retained), cleanup=lambda: None)):
                base.setUp()
        else:
            base.setUp()
        self.addCleanup(base.doCleanups)
        self.base, self.root, self.store = base, base.root, base.store
        self.script, self.images, self.review = base.base.base.workspace, base.base.workspace, base.review
        self.token = base.token
        self.scope = {"workspace_id": base.workspace_id, "project_id": base.project_id}
        self.api = f"/api/formal/projects/{base.project_id}"
        self.headers = {"X-MPlus-Agent-Auth": self.token}
        self.evidence = {"validation_surface": "real core and HTTP DTO; browser checks are separate",
                         "restart_surface": "application services and HTTP client reopened from persisted SQLite",
                         "external_provider_calls": 0, "anonymous_generation_calls": len(base.base.base.calls),
                         "completed_stages": [], "project_id": base.project_id}
        self.client, self.agent = self._make_client()
        try:
            baseline_review = self._confirm_review("baseline")
            initial = self._json("/script")
            initial_images = self._json("/image-workspace")
            original = self._artifacts()
            count = len(initial["shots"])
            self.assertGreaterEqual(count, 8)
            self.assertGreaterEqual(len({d["image_sha256"] for d in original["image_decisions"]["decisions"]}), 3)
            baseline_excel = self.review.prepare_excel_delivery(self.token, **self.scope,
                expected_project_version=baseline_review["project_version"])
            baseline_excel_sha = sha256_file(baseline_excel.path)
            original_quality = self._quality_image_decisions(initial["base_version_id"])
            self.assertEqual(len(original_quality), count)
            original_quality_hashes = {key: sha256_file(self._quality_evidence_path(item["evidence_sha256"]))
                                       for key, item in original_quality.items()}
            self._stage("confirmed_saved_baseline", shot_count=count,
                        baseline_version_label=initial["version_label"], baseline_version_id=initial["base_version_id"])

            sound = "持续底乐降低至衬底音量，先保留来宾交谈与脚步声，再顺接下一镜现场声。"
            sound_draft = self._change(initial, shot=initial["shots"][2], field="sound", value=sound,
                                       key="sound-third-draft")
            overview, script_dto, review_dto = (self._json(path) for path in ("/overview", "/script", "/review"))
            self.assertTrue(overview["draft"]["can_save_version"])
            self.assertEqual(overview["next_action"]["label"], "保存当前稿")
            self.assertEqual(overview["saved_delivery"]["version_label"], initial["version_label"])
            self.assertIn(initial["version_label"], overview["saved_delivery"]["label"])
            self.assertTrue(script_dto["can_save_version"])
            self.assertEqual(script_dto["base_version_id"], initial["base_version_id"])
            self.assertEqual(review_dto["base_version_id"], initial["base_version_id"])
            self.assertEqual(review_dto["state"], "confirmed")
            self.assertEqual(review_dto["version_label"], initial["version_label"])
            for index, (before, after) in enumerate(zip(initial["shots"], script_dto["shots"])):
                expected = {**before, "sound": sound} if index == 2 else before
                self.assertEqual(after, expected)
            old_delivery = self.review.prepare_excel_delivery(self.token, **self.scope,
                expected_project_version=review_dto["project_version"])
            self.assertEqual(sha256_file(old_delivery.path), baseline_excel_sha)
            self.assertEqual(self._artifacts(), original)
            self._stage("staged_sound_draft_prioritized_over_labelled_old_delivery",
                        staged_http_dtos={"overview": overview, "script": script_dto, "review": review_dto})

            stale_save = self.client.post(self.api + "/script/save", headers=self.headers,
                params={"workspace_id": base.workspace_id}, json={
                    "expected_project_version": sound_draft["project_version"],
                    "expected_revision": sound_draft["revision"] - 1,
                    "reason": "用过期草稿修订验证失败不会伪称保存成功", "idempotency_key": "stale-sound-save"})
            self.assertEqual(stale_save.status_code, 409, stale_save.text)
            self.assertIn("DRAFT_REVISION_CONFLICT", stale_save.text)
            self.assertEqual(self._json("/script"), script_dto)
            self.assertEqual(self._artifacts(), original)
            self._stage("stale_save_conflict_preserves_staged_draft_and_frozen_version",
                        rejected_save={"status": stale_save.status_code, "error": stale_save.json()})

            sound_saved = self._save(sound_draft, "只调整第3镜声音并保留逐镜图片", "save-sound")
            sound_images = self._json("/image-workspace")
            self.assertEqual(sound_images["state"], "confirmed")
            for before, after in zip(initial_images["shots"], sound_images["shots"]):
                for field in ("selected_image_key", "confirmed", "decision_kind", "temporary_reason", "reuse_reason"):
                    self.assertEqual(after.get(field), before.get(field), field)
            sound_artifacts = self._artifacts()
            self.assertEqual(sound_artifacts["image_decisions"]["base_version_id"], original["version_id"])
            for before, after in zip(original["image_decisions"]["decisions"], sound_artifacts["image_decisions"]["decisions"]):
                self._same_image_decision(before, after)
            self.assertEqual(sha256_file(Path(original["script_draft_path"])), original["script_draft_sha256"])
            sound_review = self._json("/review")
            self.assertIsNone(sound_review["final_confirmation"])
            changed = [item for item in sound_review["issues"] if item["kind"] == "change"]
            self.assertTrue(any(item["shot_no"] == "3" and "声音" in item["change_summary"] for item in changed), changed)
            sound_issue = next(item for item in changed if item["shot_no"] == "3")
            self.assertEqual(sound_issue["affected_adjacent_edges"], ["2->3", "3->4"])
            self.review.start_review(self.token, **self.scope,
                expected_project_version=sound_review["project_version"], idempotency_key="sound-review-inheritance")
            sound_review = self._json("/review")
            quality_records = self._quality_image_decisions(sound_review["base_version_id"])
            inherited_items = {item["shot_no"]: item for item in sound_review["issues"] if item["kind"] == "image"}
            self.assertEqual(set(inherited_items), set(original_quality))
            for shot_no, original_decision in original_quality.items():
                item, inherited = inherited_items[shot_no], quality_records[shot_no]
                self.assertEqual(item["status"], "approve")
                self.assertEqual(item["inherited_review"]["version_label"], initial["version_label"])
                self.assertTrue(item["inherited_review"]["reviewer"])
                self.assertEqual(item["inherited_review"]["reviewed_at"], original_decision["decided_at"])
                self.assertEqual(inherited["reviewer_actor_id"], original_decision["reviewer_actor_id"])
                self.assertEqual(inherited["decided_at"], original_decision["decided_at"])
                self.assertEqual(inherited["decision"], "approve")
                wrapper = json.loads(self._quality_evidence_path(inherited["evidence_sha256"]).read_text(encoding="utf-8"))
                self.assertIn(original_decision["evidence_ref"], json.dumps(wrapper, ensure_ascii=False))
                self.assertIn(original_decision["evidence_sha256"], json.dumps(wrapper, ensure_ascii=False))
                self.assertEqual(sha256_file(self._quality_evidence_path(original_decision["evidence_sha256"])),
                                 original_quality_hashes[shot_no])
            open_changes = [item for item in sound_review["issues"] if item["kind"] == "change"]
            self.assertEqual([item["shot_no"] for item in open_changes], ["3"])
            self.assertEqual(open_changes[0]["status"], "open")
            self.assertEqual(open_changes[0]["affected_adjacent_edges"], ["2->3", "3->4"])
            self.assertIsNone(sound_review["final_confirmation"])
            self.assertFalse(sound_review["ready_for_final"])
            self.assertEqual(self._quality_image_decisions(initial["base_version_id"]), original_quality)
            self._stage("sound_child_keeps_all_image_decisions_and_requires_new_review",
                        sound_version_id=sound_saved["base_version_id"], sound_review=sound_review,
                        inherited_quality_review_count=len(inherited_items))

            # A cancelled fourth-shot suggestion must never enter the draft or
            # surviving original basis. The editor only proposes anonymous data.
            before_original = self._json("/script")
            targets = before_original["shots"][2:4]
            editor_calls = []
            def editor(prompt, model, max_tokens):
                editor_calls.append({"model": model, "prompt": prompt})
                return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1",
                    "operations": [{"shot_id": shot["shot_id"], "field": field, "value": value}
                        for shot in targets for field, value in (
                            ("camera_type", "地面"), ("shot_size", "特写"), ("camera_move", "跟随"),
                            ("visual", "地面镜头跟随原镜人物的入场动作，以手部与步伐特写呈现同一过程，保留原有事件和时长。"))],
                    "shot_reasons": [{"shot_id": shot["shot_id"], "reason": "以近距离动作细节承接现场过程，保持原有事件与时长。"}
                                     for shot in targets]}, ensure_ascii=False)
            revision = FormalScriptRevisionService(projects=ProjectService(self.store), tasks=TaskService(self.store),
                workspace=self.script, image_workspace=self.images, writes_enabled=True,
                assistant_editor_policy=FormalScriptAssistantEditorPolicy(editor, "anonymous", "anonymous-editor"))
            self.client, self.agent = self._make_client(revision)
            proposal = revision.propose(self.token, agent_session_id="", project_id=base.project_id,
                instruction="减少航拍，把这两镜改为地面跟随动作的特写，保持原有事件与时长。",
                selected_shot_ids=[shot["shot_id"] for shot in targets],
                expected_project_version=before_original["project_version"], expected_revision=0,
                idempotency_key="original-two-shots", draft_only=True, entrypoint="assistant")
            self.assertEqual(proposal["response"]["type"], "action_proposal", proposal)
            retained = revision.reselect_proposal(self.token, project_id=base.project_id,
                task_id=proposal["task_id"], retained_shot_ids=[targets[0]["shot_id"]],
                expected_project_version=before_original["project_version"], expected_revision=0,
                idempotency_key="original-third-only")
            self.assertEqual(len(editor_calls), 1)
            self.assertEqual(TaskService(self.store).get_task(self.token, proposal["task_id"])["status"], "cancelled")
            self.assertEqual(retained["response"]["affected_shots"], ["3"])
            applied = self.agent.confirm_action(self.token, retained["task_id"],
                proposal_digest=retained["response"]["proposal_digest"])
            self.assertEqual(applied["task"]["status"], "completed", applied)
            original_draft = self._json("/script")
            self.assertEqual(original_draft["shots"][2]["adaptation"]["origin"], "original_suggestion")
            self.assertIn("特写", original_draft["shots"][2]["adaptation"]["summary"])
            for index, (before, after) in enumerate(zip(before_original["shots"], original_draft["shots"])):
                if index != 2:
                    self.assertEqual(after, before)

            abandoned = "废弃分支：改用不连续的侧面取景。"
            branch = self._change(original_draft, shot=original_draft["shots"][2], field="visual", value=abandoned,
                                  key="original-abandoned-visual")
            self.assertIn(abandoned, branch["shots"][2]["adaptation"]["summary"])
            undone = self.script.undo(self.token, **self.scope, expected_project_version=branch["project_version"],
                expected_revision=branch["revision"], idempotency_key="original-undo-branch")
            self.assertEqual(undone["shots"], original_draft["shots"])
            redone = self.script.redo(self.token, **self.scope, expected_project_version=undone["project_version"],
                expected_revision=undone["revision"], idempotency_key="original-redo-branch")
            self.assertEqual(redone["shots"], branch["shots"])
            undone = self.script.undo(self.token, **self.scope, expected_project_version=redone["project_version"],
                expected_revision=redone["revision"], idempotency_key="original-undo-branch-again")
            final_visual = "跟随来宾步入现场的手部动作，以特写呈现真实交流细节，延续原镜头的事件顺序。"
            final_draft = self._change(undone, shot=undone["shots"][2], field="visual", value=final_visual,
                                       key="original-final-visual")
            self.assertFalse(final_draft["can_redo"])
            self.assertNotIn(abandoned, json.dumps(final_draft, ensure_ascii=False))
            self.assertIn(final_visual, final_draft["shots"][2]["adaptation"]["summary"])
            self._stage("cancel_continue_undo_redo_and_new_branch_keep_original_basis",
                        original_proposal=proposal["response"], retained_proposal=retained["response"],
                        current_original_summary=final_draft["shots"][2]["adaptation"])

            image_ready = self._json("/image-workspace")
            target = image_ready["shots"][2]
            choice = next(item for item in target["candidates"] if item["image_key"] != initial_images["shots"][2]["selected_image_key"])
            progress = self.images.read_progress(self.token, **self.scope)
            versions_before = ProjectService(self.store).list_project_versions(self.token, base.project_id)
            self.images.save_progress(self.token, **self.scope,
                base_version_id=final_draft["base_version_id"], expected_project_version=final_draft["project_version"],
                expected_revision=progress["revision"], expected_script_revision=final_draft["revision"],
                selections=[dict(shot_id=target["shot_id"], image_key=choice["image_key"], checked=True,
                    reuse_reason="已核对原创特写的参考用途")], idempotency_key="original-third-image-progress")
            self.assertEqual(ProjectService(self.store).list_project_versions(self.token, base.project_id), versions_before)
            original_saved = self._save(final_draft, "保存第3镜原创特写和后续画面编辑", "save-original")
            original_child = self._artifacts(include_decisions=False)
            detail = original_child["script_plan"]["detail_rows"][2]
            basis = json.loads(detail["原创改编依据"])
            self.assertEqual(basis["fields"]["shot_size"], "特写")
            self.assertEqual(basis["fields"]["visual"], final_visual)
            self.assertNotIn(abandoned, json.dumps(basis, ensure_ascii=False))
            self.assertEqual(original_saved["shots"][2]["source"], initial["shots"][2]["source"])
            image_saved = original_saved
            image_ready = self._json("/image-workspace")
            self.assertEqual(image_ready["pending_reconfirmation_shot_nos"], [])
            self.assertEqual(sum(shot["confirmed"] for shot in image_ready["shots"]), count)
            self.assertEqual(image_ready["shots"][2]["selected_image_key"], choice["image_key"])
            delivered_artifacts = self._artifacts()
            for index, (before, after) in enumerate(zip(sound_artifacts["image_decisions"]["decisions"],
                                                       delivered_artifacts["image_decisions"]["decisions"])):
                if index != 2:
                    if "inherited_from_version_id" in after:
                        self.assertEqual(after["inherited_from_version_id"], sound_artifacts["version_id"])
                    self._same_image_decision(before, {k: v for k, v in after.items() if k != "inherited_from_version_id"})
            self.assertEqual(self._json("/script")["shots"][2]["adaptation"], original_saved["shots"][2]["adaptation"])
            self._stage("saved_original_version_reconfirms_only_target_image", image_version_id=image_saved["base_version_id"])

            final = self._confirm_review("original-final")
            excel = self.review.prepare_excel_delivery(self.token, **self.scope,
                expected_project_version=final["project_version"])
            pdf = self.review.prepare_pdf_delivery(self.token, **self.scope,
                expected_project_version=final["project_version"])
            self.assertEqual(pdf.status, "available", pdf.unavailable_reason)
            self.evidence["generated_formal_exports"] = {"excel_path": str(excel.path), "pdf_path": str(pdf.path),
                "excel_sha256": sha256_file(excel.path), "pdf_sha256": sha256_file(pdf.path)}
            workbook = load_workbook(excel.path, data_only=True)
            try:
                self.assertEqual(len(workbook["脚本"]._images), count)
                self.assertEqual(workbook[ADJACENT_REVIEW_SHEET_NAME].max_row - 1, count - 1)
                adjacent = workbook[ADJACENT_REVIEW_SHEET_NAME]
                adjacent_headers = {str(cell.value): cell.column for cell in adjacent[1]}
                adjacent_rows = {str(adjacent.cell(row, adjacent_headers["边"]).value): row
                                 for row in range(2, adjacent.max_row + 1)}
                self.assertEqual(adjacent.cell(adjacent_rows["2->3"], adjacent_headers["后镜声音"]).value, sound)
                self.assertEqual(adjacent.cell(adjacent_rows["3->4"], adjacent_headers["前镜声音"]).value, sound)
                workbook_text = "\n".join(str(cell.value or "") for sheet in workbook for row in sheet for cell in row)
                self.assertIn("原创拍摄建议", workbook_text)
                self.assertIn(final_visual, workbook_text)
                self.assertNotIn(abandoned, workbook_text)
            finally:
                workbook.close()
            reader = PdfReader(str(pdf.path))
            self.assertEqual(len(reader.pages), count)
            pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
            self.assertIn("原创拍摄建议", pdf_text)
            self.assertIn("特写", pdf_text)
            self.assertIn("".join(final_visual.split()), "".join(pdf_text.split()))
            self.assertNotIn(abandoned, pdf_text)
            versions = ProjectService(self.store).list_project_versions(self.token, base.project_id)
            review_book = self.script.build_review_workbook(self.token, **self.scope, version_no=len(versions))
            source = RunArtifactResolver(self.root / "runs").resolve_artifact(versions[-1]["run_id"], "workbook").path
            _assert_review_workbook_matches_saved_version(BytesIO(review_book["content"]), source, count, len(versions))
            (self.root / review_book["filename"]).write_bytes(review_book["content"])
            excel_sha, pdf_sha = sha256_file(excel.path), sha256_file(pdf.path)
            self._stage("new_final_confirmation_formal_excel_pdf_and_separate_review_copy",
                excel_path=str(excel.path), excel_sha256=excel_sha, pdf_path=str(pdf.path), pdf_sha256=pdf_sha,
                version_count=len(versions), final_version_id=versions[-1]["version_id"])

            # Rebuild service objects from the real persisted workspace. No state
            # or business rows are replaced to simulate a restart.
            self.client.close()
            self.store = WorkspaceStore(self.store.database_path)
            projects, artifacts = ProjectService(self.store), RunArtifactResolver(self.root / "runs")
            delivery = VersionDeliveryService(self.store, artifacts)
            image_services = replace(self.images.services, store=self.store, projects=projects, delivery=delivery,
                artifacts=artifacts, images=ProjectImagePoolService(self.store, data_root=self.root / "runtime"))
            self.images = FormalImageWorkspaceService(image_services, runs_root=self.root / "runs",
                source_root=base.base.base.generated.source_root)
            base.base.workspace = self.images
            self.review = base._review_service(self.store)
            self.script = FormalScriptWorkspaceService(replace(self.script.services, store=self.store,
                projects=projects, delivery=delivery, artifacts=artifacts, commands=self.review.services.commands,
                image_workspace=self.images), runs_root=self.root / "runs", source_root=base.base.base.generated.source_root)
            self.client, self.agent = self._make_client()
            restored = self._json("/script")
            self.assertFalse(restored["can_save_version"])
            self.assertEqual(restored["shots"][2]["adaptation"], original_saved["shots"][2]["adaptation"])
            self.assertNotIn(abandoned, json.dumps(restored, ensure_ascii=False))
            self.assertEqual(self._artifacts(), delivered_artifacts)
            restored_review = self._json("/review")
            self.assertEqual(restored_review["state"], "confirmed")
            for suffix, expected_sha in (("excel", excel_sha), ("pdf", pdf_sha)):
                response = self.client.get(self.api + "/review/" + suffix, headers=self.headers,
                    params={"workspace_id": base.workspace_id, "expected_project_version": restored_review["project_version"]})
                self.assertEqual(response.status_code, 200, response.text[:500])
                path = self.root / ("restarted-delivery." + ("xlsx" if suffix == "excel" else "pdf"))
                path.write_bytes(response.content)
                self.assertEqual(sha256_file(path), expected_sha)
            self.assertEqual(len(base.base.base.calls), 2)
            self._stage("persisted_restart_keeps_final_basis_decisions_and_both_export_hashes")
        except Exception as exc:
            self.evidence["failure"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            (self.root / "creative-version-result.json").write_text(
                json.dumps(self.evidence, ensure_ascii=False, indent=2), encoding="utf-8")
