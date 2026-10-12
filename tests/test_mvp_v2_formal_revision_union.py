from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentServices
from mvp_v2.formal_agent import FormalExecutableAgentService, FormalExecutionBackend
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
from mvp_v2.formal_script_revision import FormalScriptRevisionService
from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import create_app
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_script_workspace as script_workspace_tests
from tests.test_mvp_v2_formal_agent import FakeFormalGeneration
from tests import test_mvp_v2_formal_image_workspace as image_workspace_tests


class FormalRevisionUnionTests(unittest.TestCase):
    def test_batch_with_invalid_second_target_does_not_apply_first(self):
        before = self.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        response = self.client.post(
            f"/api/formal/projects/{self.project_id}/script/draft-proposals?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "expected_project_version": before["project_version"],
                "expected_revision": before["revision"],
                "instruction": "把第2镜字幕修改为活动开场；第999镜字幕修改为活动结束",
                "idempotency_key": "invalid-semicolon-target",
                "entrypoint": "assistant", "feedback_kind": "copy",
            },
        )
        self.assertGreaterEqual(response.status_code, 400)
        after = self.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["shots"], before["shots"])

    def test_quoted_subtitle_is_not_split_into_another_command(self):
        parsed = FormalScriptRevisionService.parse_instruction(
            '把第2镜字幕修改为“活动开场；第3镜字幕修改为活动结束”'
        )
        self.assertEqual(parsed["shot_no"], "2")
        self.assertEqual(parsed["value"], '“活动开场；第3镜字幕修改为活动结束”')

    def test_semicolon_changes_are_one_atomic_draft_revision(self):
        path = f"/api/formal/projects/{self.project_id}/script"
        before = self.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        versions = self.projects.list_project_versions(self.token, self.project_id)
        proposal = self.client.post(
            f"{path}/draft-proposals?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "expected_project_version": before["project_version"],
                "expected_revision": before["revision"],
                "instruction": "把第2镜字幕修改为活动开场；第3镜字幕修改为活动进行中",
                "idempotency_key": "semicolon-two-shots",
                "entrypoint": "assistant", "feedback_kind": "copy",
            },
        )
        self.assertEqual(proposal.status_code, 200, proposal.text)
        body = proposal.json()
        confirmed = self.client.post(
            f"/api/agent/tasks/{body['task_id']}/confirm", headers=self.headers,
            json={"proposal_digest": body["response"]["proposal_digest"]},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        after = self.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["shots"][1]["subtitle"], "活动开场")
        self.assertEqual(after["shots"][2]["subtitle"], "活动进行中")
        self.assertEqual(after["shots"][0], before["shots"][0])
        self.assertEqual(after["shots"][3:], before["shots"][3:])
        self.assertEqual(self.projects.list_project_versions(self.token, self.project_id), versions)

    def setUp(self):
        self.images = None
        if self._testMethodName == "test_existing_draft_save_requires_proposal_and_replays_one_child":
            fixture = image_workspace_tests.FormalImageWorkspaceTests("runTest")
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            base, self.images = fixture.base, fixture.workspace
        else:
            base = script_workspace_tests.FormalScriptWorkspaceTests("runTest")
            base.setUp()
            self.addCleanup(base.doCleanups)
        self.base = base
        self.store = base.store
        self.projects = ProjectService(self.store)
        self.tasks = TaskService(self.store)
        self.project_id = base.generated.project["project_id"]
        self.workspace_id = base.generated.workspace_id
        self.token = base.generated.token
        self.adapter = FormalBriefInputAdapter(
            base.generated.briefs,
            data_root=base.generated.root / "runtime",
        )
        self.brief_store = BriefArtifactStore(base.generated.root / "runtime")
        readonly = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store,
            identity=LocalIdentityAdapter(self.store),
            projects=self.projects,
            tasks=self.tasks,
            versions=VersionService(self.store),
            artifacts=RunArtifactResolver(base.generated.root / "runs"),
        ))
        self.revision = FormalScriptRevisionService(
            projects=self.projects,
            tasks=self.tasks,
            workspace=base.workspace,
            image_workspace=self.images,
            writes_enabled=True,
            brief_adapter=self.adapter,
            brief_store=self.brief_store,
        )
        self.agent = FormalExecutableAgentService(
            ExecutableAgentServices(
                readonly=readonly,
                projects=self.projects,
                tasks=self.tasks,
                confirmations=ActionConfirmationService(self.store, b"u" * 32),
                idempotency=ToolIdempotencyService(self.store),
                brief_store=self.brief_store,
                backend=FormalExecutionBackend(),
            ),
            generation=FakeFormalGeneration(),
            advisor=None,
            writes_enabled=True,
            script_revision=self.revision,
        )
        self.client = TestClient(create_app(
            agent_api=self.agent,
            formal_brief_adapter=self.adapter,
            formal_brief_upload_factory=lambda filename, stream, critical: UploadedDocument(
                filename=filename, stream=stream, critical=critical,
            ),
            formal_script_service=base.workspace,
        ))
        self.addCleanup(self.client.close)
        self.headers = {
            "X-MPlus-Agent-Auth": self.token,
            "X-MPlus-UI-Token": self.client.app.state.session_token,
        }

    def test_existing_draft_save_requires_proposal_and_replays_one_child(self):
        path = f"/api/formal/projects/{self.project_id}/script"
        current = self.revision.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        changed = self.client.post(
            f"{path}/table?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "expected_project_version": current["project_version"],
                "expected_revision": 0,
                "shot_no": "1", "shot_id": current["shots"][0]["shot_id"],
                "field": "visual",
                "value": "匿名嘉宾从入口走向主舞台并停留在产品旁。",
                "idempotency_key": "union-draft-table-1",
            },
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["state"], "editing")
        scope = dict(workspace_id=self.workspace_id, project_id=self.project_id)
        progress = self.images.read_progress(self.token, **scope)
        selections = []
        for shot in changed.json()["shots"]:
            found = self.images.search_shot(self.token, **scope, base_version_id=current["base_version_id"],
                expected_project_version=current["project_version"], shot_no=shot["shot_no"], query="")
            self.assertTrue(found["candidates"], shot["shot_no"])
            selections.append(dict(shot_id=shot["shot_id"], image_key=found["candidates"][0]["image_key"],
                checked=True, reuse_reason="匿名验收：同场景参考已逐镜核对"))
        self.images.save_progress(self.token, **scope, base_version_id=current["base_version_id"],
            expected_project_version=current["project_version"], expected_revision=progress["revision"],
            expected_script_revision=changed.json()["revision"], selections=selections,
            idempotency_key="union-checked-images")
        before_versions = self.projects.list_project_versions(self.token, self.project_id)
        proposal = self.client.post(
            f"{path}/save?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "expected_project_version": current["project_version"],
                "expected_revision": changed.json()["revision"],
                "reason": "保存匿名入口镜头修改",
                "idempotency_key": "union-draft-save-1",
            },
        )
        self.assertEqual(proposal.status_code, 200, proposal.text)
        proposal_body = proposal.json()
        self.assertEqual(proposal_body["response"]["action"], "apply_formal_script_revision")
        repeated_proposal = self.client.post(
            f"{path}/save?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "expected_project_version": current["project_version"],
                "expected_revision": changed.json()["revision"],
                "reason": "保存匿名入口镜头修改",
                "idempotency_key": "union-draft-save-1",
            },
        )
        self.assertEqual(repeated_proposal.status_code, 200, repeated_proposal.text)
        self.assertEqual(repeated_proposal.json()["task_id"], proposal_body["task_id"])
        self.assertEqual(
            len(self.projects.list_project_versions(self.token, self.project_id)),
            len(before_versions),
        )
        confirm_path = f"/api/agent/tasks/{proposal_body['task_id']}/confirm"
        confirmation = self.client.post(
            confirm_path,
            headers=self.headers,
            json={"proposal_digest": proposal_body["response"]["proposal_digest"]},
        )
        self.assertEqual(confirmation.status_code, 200, confirmation.text)
        self.assertEqual(confirmation.json()["task"]["status"], "completed")
        after_versions = self.projects.list_project_versions(self.token, self.project_id)
        self.assertEqual(len(after_versions), len(before_versions) + 1)
        replay = self.client.post(
            confirm_path,
            headers=self.headers,
            json={"proposal_digest": proposal_body["response"]["proposal_digest"]},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(
            len(self.projects.list_project_versions(self.token, self.project_id)),
            len(after_versions),
        )
        with self.store.connection() as connection:
            evidence = connection.execute(
                """SELECT revision_report_ref, revision_report_sha256
                FROM version_review_draft_submissions
                WHERE materialized_version_id=?""",
                (after_versions[-1]["version_id"],),
            ).fetchone()
        self.assertIsNotNone(evidence)

    def test_instruction_proposal_applies_to_shared_draft_without_creating_version(self):
        path = f"/api/formal/projects/{self.project_id}/script"
        before_versions = self.projects.list_project_versions(self.token, self.project_id)
        proposal = self.client.post(
            f"{path}/draft-proposals?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "expected_project_version": self.projects.get_project(
                    self.token, self.project_id,
                )["project_version"],
                "expected_revision": 0,
                "instruction": "把第1镜字幕修改为匿名活动正式开始",
                "idempotency_key": "union-draft-instruction-1",
                "entrypoint": "assistant",
                "feedback_kind": "copy",
            },
        )
        self.assertEqual(proposal.status_code, 200, proposal.text)
        proposal_body = proposal.json()
        self.assertTrue(proposal_body["response"]["draft_only"])

        confirmation = self.client.post(
            f"/api/agent/tasks/{proposal_body['task_id']}/confirm",
            headers=self.headers,
            json={"proposal_digest": proposal_body["response"]["proposal_digest"]},
        )
        self.assertEqual(confirmation.status_code, 200, confirmation.text)
        self.assertEqual(confirmation.json()["task"]["status"], "completed")
        self.assertEqual(
            confirmation.json()["response"]["type"], "formal_script_draft_updated",
        )
        self.assertEqual(
            len(self.projects.list_project_versions(self.token, self.project_id)),
            len(before_versions),
        )

        current = self.client.get(
            f"{path}?workspace_id={self.workspace_id}", headers=self.headers,
        )
        self.assertEqual(current.status_code, 200, current.text)
        current_body = current.json()
        self.assertEqual(current_body["state"], "editing")
        self.assertEqual(current_body["revision"], 1)
        self.assertEqual(current_body["shots"][0]["subtitle"], "匿名活动正式开始")
        self.assertTrue(current_body["can_undo"])

    def test_semantic_instruction_proposal_confirms_without_leaking_shot_identity(self):
        path = f"/api/formal/projects/{self.project_id}/script"
        current = self.revision.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        shot = current["shots"][0]
        current = self.revision.workspace.apply_table_change(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=current["project_version"],
            expected_revision=0,
            shot_no=str(shot["shot_no"]), shot_id=shot["shot_id"],
            field="voiceover",
            value="在这个充满无限可能的时刻，让我们共同见证活动启幕。",
            idempotency_key="union-semantic-seed-1",
        )
        self.revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=lambda _prompt, _model, _max_tokens: json.dumps({
                "schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [{
                    "shot_id": shot["shot_id"],
                    "field": "voiceover",
                    "value": "嘉宾走进会场，活动现在开始。",
                }],
            }, ensure_ascii=False),
            provider="deepseek-http",
            model="deepseek-v4-flash",
        )
        before_versions = self.projects.list_project_versions(self.token, self.project_id)

        proposal = self.client.post(
            f"{path}/draft-proposals?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "expected_project_version": current["project_version"],
                "expected_revision": current["revision"],
                "instruction": "第1镜旁白更自然",
                "idempotency_key": "union-semantic-draft-1",
                "entrypoint": "assistant",
                "feedback_kind": "copy",
            },
        )
        self.assertEqual(proposal.status_code, 200, proposal.text)
        proposal_body = proposal.json()
        confirmation = self.client.post(
            f"/api/agent/tasks/{proposal_body['task_id']}/confirm",
            headers=self.headers,
            json={"proposal_digest": proposal_body["response"]["proposal_digest"]},
        )

        self.assertEqual(confirmation.status_code, 200, confirmation.text)
        self.assertEqual(confirmation.json()["task"]["status"], "completed")
        updated = self.revision.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual(updated["revision"], current["revision"] + 1)
        self.assertEqual(updated["shots"][0]["voiceover"], "嘉宾走进会场，活动现在开始。")
        self.assertEqual(
            self.projects.list_project_versions(self.token, self.project_id), before_versions,
        )
        replay = self.client.post(
            f"/api/agent/tasks/{proposal_body['task_id']}/confirm",
            headers=self.headers,
            json={"proposal_digest": proposal_body["response"]["proposal_digest"]},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        replayed = self.revision.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual(replayed["revision"], updated["revision"])

    def test_fact_revision_creates_only_pending_brief_child_and_feedback_artifact(self):
        project = self.projects.get_project(self.token, self.project_id)
        base_script_version = project["current_version_id"]
        parent = self.adapter.read_current_formal(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
        )
        with self.store.connection() as connection:
            brief_count = connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0]
            feedback_count = connection.execute(
                "SELECT COUNT(*) FROM formal_version_feedback_records WHERE feedback_kind='fact'",
            ).fetchone()[0]
        path = f"/api/formal/projects/{self.project_id}/fact-revision-proposals"
        proposal = self.client.post(
            f"{path}?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "expected_project_version": project["project_version"],
                "field": "必须出现的内容",
                "value": "匿名产品演示与嘉宾互动",
                "evidence": "匿名验收记录第 2 项，经项目经理人工核对",
                "idempotency_key": "union-fact-revision-1",
            },
        )
        self.assertEqual(proposal.status_code, 200, proposal.text)
        proposed = proposal.json()
        self.assertEqual(proposed["response"]["action"], "apply_formal_fact_revision")
        self.assertEqual(proposed["response"]["before"], "核心宣讲与仪式")
        repeated_proposal = self.client.post(
            f"{path}?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "expected_project_version": project["project_version"],
                "field": "必须出现的内容",
                "value": "匿名产品演示与嘉宾互动",
                "evidence": "匿名验收记录第 2 项，经项目经理人工核对",
                "idempotency_key": "union-fact-revision-1",
            },
        )
        self.assertEqual(repeated_proposal.status_code, 200, repeated_proposal.text)
        self.assertEqual(repeated_proposal.json()["task_id"], proposed["task_id"])
        confirmation = self.client.post(
            f"{path}/{proposed['task_id']}/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"proposal_digest": proposed["response"]["proposal_digest"]},
        )
        self.assertEqual(confirmation.status_code, 200, confirmation.text)
        completed = confirmation.json()
        self.assertEqual(completed["type"], "formal_fact_revision_completed")
        self.assertTrue(completed["brief_confirmation_required"])
        self.assertFalse(completed["planning_started"])
        current = self.projects.get_project(self.token, self.project_id)
        self.assertEqual(current["current_version_id"], base_script_version)
        self.assertEqual(current["current_brief_snapshot_id"], project["current_brief_snapshot_id"])
        with self.store.connection() as connection:
            brief = connection.execute(
                """SELECT v.parent_brief_version_id,
                (SELECT COUNT(*) FROM brief_confirmations c
                 WHERE c.brief_version_id=v.brief_version_id) AS confirmation_count
                FROM brief_versions v WHERE v.brief_version_id=?""",
                (completed["brief_version_id"],),
            ).fetchone()
            rows = connection.execute(
                """SELECT payload_ref, payload_sha256 FROM formal_version_feedback_records
                WHERE feedback_kind='fact' ORDER BY created_at""",
            ).fetchall()
            final_brief_count = connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0]
        self.assertEqual(brief["parent_brief_version_id"], parent["brief_key"])
        self.assertEqual(brief["confirmation_count"], 0)
        self.assertEqual(final_brief_count, brief_count + 1)
        self.assertEqual(len(rows), feedback_count + 1)
        artifact = self.brief_store.read(rows[-1]["payload_ref"], rows[-1]["payload_sha256"])
        self.assertEqual(artifact["child_brief_version_id"], completed["brief_version_id"])
        self.assertEqual(artifact["field"], "必须出现的内容")
        replay = self.client.post(
            f"{path}/{proposed['task_id']}/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"proposal_digest": proposed["response"]["proposal_digest"]},
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        with self.store.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0],
                final_brief_count,
            )

    def test_fact_revision_feedback_failure_rolls_back_child_input_and_artifact(self):
        project = self.projects.get_project(self.token, self.project_id)
        with self.store.connection() as connection:
            before = {
                "briefs": connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0],
                "files": connection.execute("SELECT COUNT(*) FROM project_files").fetchone()[0],
                "feedback": connection.execute(
                    "SELECT COUNT(*) FROM formal_version_feedback_records WHERE feedback_kind='fact'",
                ).fetchone()[0],
            }
        artifact_paths = set(self.brief_store.root.rglob("*.json"))
        artifact_bytes = {path: path.read_bytes() for path in artifact_paths}
        path = f"/api/formal/projects/{self.project_id}/fact-revision-proposals"
        proposal = self.client.post(
            f"{path}?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "expected_project_version": project["project_version"],
                "field": "特殊需求",
                "value": "匿名验收故障注入事实",
                "evidence": "匿名故障注入证据，仅验证原子回退",
                "idempotency_key": "union-fact-rollback-1",
            },
        )
        self.assertEqual(proposal.status_code, 200, proposal.text)
        proposed = proposal.json()
        freeze = self.brief_store.freeze
        frozen = []

        def freeze_then_fail(payload):
            frozen.append((freeze(payload), payload))
            raise WorkspaceServiceError("INJECTED_FACT_FEEDBACK_FAILURE", "故障注入")

        with patch.object(self.brief_store, "freeze", side_effect=freeze_then_fail):
            failed = self.client.post(
                f"{path}/{proposed['task_id']}/confirm?workspace_id={self.workspace_id}",
                headers=self.headers,
                json={"proposal_digest": proposed["response"]["proposal_digest"]},
            )
        self.assertNotEqual(failed.status_code, 200, failed.text)
        self.assertEqual(
            failed.json()["detail"]["code"],
            "INJECTED_FACT_FEEDBACK_FAILURE",
        )
        current = self.projects.get_project(self.token, self.project_id)
        self.assertEqual(current["project_version"], project["project_version"])
        self.assertEqual(current["current_version_id"], project["current_version_id"])
        self.assertEqual(
            current["current_brief_snapshot_id"],
            project["current_brief_snapshot_id"],
        )
        with self.store.connection() as connection:
            after = {
                "briefs": connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0],
                "files": connection.execute("SELECT COUNT(*) FROM project_files").fetchone()[0],
                "feedback": connection.execute(
                    "SELECT COUNT(*) FROM formal_version_feedback_records WHERE feedback_kind='fact'",
                ).fetchone()[0],
            }
        self.assertEqual(after, before)
        # RV06 preserves published content-addressed blobs: another request may
        # already reference them. Roll back business bindings, never shared bytes.
        self.assertEqual(len(frozen), 1)
        artifact, payload = frozen[0]
        self.assertEqual(self.brief_store.read(artifact['brief_ref'], artifact['brief_sha256']), payload)
        self.assertEqual({path: path.read_bytes() for path in artifact_paths}, artifact_bytes)
        digest = artifact['brief_sha256']
        self.assertEqual(set(self.brief_store.root.rglob("*.json")),
                         artifact_paths | {self.brief_store.root / digest[:2] / (digest + '.json')})
        self.assertEqual(freeze(payload), artifact)
        self.assertEqual(set(self.brief_store.root.rglob("*.json")),
                         artifact_paths | {self.brief_store.root / digest[:2] / (digest + '.json')})


if __name__ == "__main__":
    unittest.main()
