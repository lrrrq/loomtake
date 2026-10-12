from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.alpha_brief import AlphaBriefStore
from mvp_v2.alpha_console import AlphaConsole, AlphaRuntime, create_alpha_app
from mvp_v2.alpha_integration import AlphaProjectFlow
from mvp_v2.alpha_image_flow import AlphaImageFlow
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentService, ExecutableAgentServices
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.models import SourceShot
from mvp_v2.planning_core import compile_plan_artifact
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceStore


class _SnapshotBackend:
    def __init__(self, briefs: BriefArtifactStore):
        self.briefs = briefs
        self.start_count = 0
        self.seen_brief: dict | None = None

    def inspect_action(self, action, parameters, brief_snapshot):
        if action == "start_generation":
            self.seen_brief = self.briefs.read(
                brief_snapshot["brief_ref"], brief_snapshot["brief_sha256"]
            )
        return {"passed": True, "calls_models": False, "checks": []}

    def start_action(self, action, parameters, brief_snapshot):
        self.start_count += 1
        return {"job_id": "job_alpha_integration_0001", "state": "running", "progress": {}}

    def get_job(self, job_id):
        return {"job_id": job_id, "state": "running", "progress": {}}

    def cancel_job(self, job_id):
        return {"job_id": job_id, "state": "cancelled", "progress": {}}

    def review_readiness(self, run_id):
        return {"ready": False, "run_id": run_id, "summary": "Alpha 不导出复核包"}

    def prepare_human_review(self, run_id):
        raise AssertionError("Alpha 集成测试不导出复核包")

    def finalized_artifacts(self, snapshot):
        return None


class AlphaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.runtime_temp = tempfile.TemporaryDirectory(prefix="mplus-alpha-integration-")
        self.addCleanup(self.runtime_temp.cleanup)
        self.runtime = AlphaRuntime.isolated(Path(self.runtime_temp.name) / "runtime", port=18766)
        store = WorkspaceStore(self.runtime.data_root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名 Alpha 接线工作区",
            display_name="匿名审核人",
            confirmed=True,
            token="alpha-integration-owner",
        )
        self.token = owner["auth_token"]
        self.actor_id = owner["actor_id"]
        self.workspace_id = owner["workspace_id"]
        readonly = ReadonlyAgentService(
            ReadonlyAgentServices(
                store=store,
                identity=self.identity,
                projects=ProjectService(store),
                tasks=TaskService(store),
                versions=VersionService(store),
                artifacts=RunArtifactResolver(self.runtime.runs_root),
            )
        )
        self.briefs = BriefArtifactStore(self.runtime.data_root)
        self.backend = _SnapshotBackend(self.briefs)
        self.agent = ExecutableAgentService(
            ExecutableAgentServices(
                readonly=readonly,
                projects=readonly.services.projects,
                tasks=readonly.services.tasks,
                confirmations=ActionConfirmationService(store, b"i" * 32),
                idempotency=ToolIdempotencyService(store),
                brief_store=self.briefs,
                backend=self.backend,
            )
        )
        self.alpha_root = Path(tempfile.mkdtemp(prefix="mplus-alpha-"))
        self.alpha_briefs = AlphaBriefStore(self.alpha_root)
        self.flow = AlphaProjectFlow(
            projects=readonly.services.projects,
            identity=self.identity,
            alpha_briefs=self.alpha_briefs,
            normalized_briefs=self.briefs,
        )
        self.images = AlphaImageFlow(
            projects=readonly.services.projects,
            identity=self.identity,
            alpha_briefs=self.alpha_briefs,
        )
        self.console = AlphaConsole(
            self.runtime,
            self.agent,
            verified_brief_reader=lambda *_args: {"unused": True},
            project_flow=self.flow,
            image_flow=self.images,
        )
        self.client = TestClient(create_alpha_app(self.console))
        self.headers = {"X-MPlus-Agent-Auth": self.token}

    def test_anonymous_project_to_confirmed_snapshot_to_planning_and_generation(self):
        created = self.client.post(
            "/api/alpha/projects",
            headers=self.headers,
            json={
                "workspace_id": self.workspace_id,
                "expected_workspace_version": 0,
                "name": "匿名接线项目",
                "category_id": "launch",
            },
        )
        self.assertEqual(created.status_code, 200)
        project = created.json()
        project_id = project["project_id"]
        self.assertIsNone(self.agent.services.projects.get_current_brief_snapshot(self.token, project_id))

        self.alpha_briefs.inputs_root.mkdir()
        fixture = self.alpha_briefs.inputs_root / "brief.txt"
        fixture.write_text(
            "项目名称: 匿名接线项目\n脚本类型: 发布会\n目标时长: 60秒\n"
            "风格调性: 清晰\n必须出现的内容: 匿名产品演示\n关键事件: 开场、演示、收束",
            encoding="utf-8",
        )
        documents = [{"path": "brief.txt", "critical": True}]
        linked = self.alpha_briefs.inputs_root / "linked-brief.txt"
        linked.symlink_to(fixture.name)
        symlink_rejected = self.client.post(
            f"/api/alpha/projects/{project_id}/brief-inputs/freeze",
            headers=self.headers,
            json={"documents": [{"path": linked.name, "critical": True}]},
        )
        self.assertEqual(symlink_rejected.status_code, 409)
        self.assertEqual(symlink_rejected.json()["detail"]["code"], "ALPHA_BRIEF_UNAVAILABLE")
        frozen = self.client.post(
            f"/api/alpha/projects/{project_id}/brief-inputs/freeze",
            headers=self.headers,
            json={"documents": documents},
        )
        self.assertEqual(frozen.status_code, 200)
        imported = self.client.post(
            f"/api/alpha/projects/{project_id}/brief-imports",
            headers=self.headers,
            json={"documents": documents},
        )
        self.assertEqual(imported.status_code, 200)
        bound = self.client.post(
            f"/api/alpha/projects/{project_id}/brief-confirmations",
            headers=self.headers,
            json={
                "imported": imported.json(),
                "expected_project_version": project["project_version"],
            },
        )
        self.assertEqual(bound.status_code, 200)
        confirmed = self.alpha_briefs.read_confirmed(
            project_id,
            bound.json()["confirmed_brief"]["brief_ref"],
            bound.json()["confirmed_brief"]["brief_sha256"],
        )
        self.assertEqual(confirmed["confirmed_by"], self.actor_id)
        snapshot = bound.json()["snapshot"]
        normalized = self.briefs.read(snapshot["brief_ref"], snapshot["brief_sha256"])
        self.assertEqual(normalized["Alpha Brief引用"], bound.json()["confirmed_brief"]["brief_ref"])
        self.assertEqual(normalized["Brief来源 SHA256"], bound.json()["confirmed_brief"]["brief_sha256"])
        self.assertTrue(normalized["Brief来源证据"])

        plan = compile_plan_artifact(
            normalized,
            [SourceShot("发布会", "anonymous-template.xlsx", "template", 1, visual_desc="匿名产品")],
            source_view="alpha-confirmed",
        )
        self.assertEqual(plan.duration_budget["planned_seconds"], 60)
        self.assertGreaterEqual(plan.duration_budget["shot_count"], 1)

        self.assertEqual(
            self.client.post("/api/agent/projects/onboard", headers=self.headers).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/alpha/projects/{project_id}/enter", headers=self.headers
            ).status_code,
            200,
        )
        raw_rejected = self.client.post(
            f"/api/alpha/projects/{project_id}/sessions",
            headers=self.headers,
            json={"brief": {"项目名称": "绕过"}, "confirmed": True},
        )
        self.assertEqual(raw_rejected.status_code, 409)
        self.assertEqual(raw_rejected.json()["detail"]["code"], "ALPHA_PROJECT_ONLY")
        session = self.client.post(
            f"/api/alpha/projects/{project_id}/sessions", headers=self.headers
        )
        self.assertEqual(session.status_code, 200)
        proposal = self.console.submit_message(
            self.token,
            agent_session_id=session.json()["session"]["agent_session_id"],
            content="请开始生成一版脚本",
        )
        running = self.console.confirm_low_risk_action(
            self.token,
            task_id=proposal["task"]["task_id"],
            proposal_digest=proposal["response"]["proposal_digest"],
        )
        self.assertEqual(running["job"]["job_id"], "job_alpha_integration_0001")
        self.assertEqual(self.backend.start_count, 1)
        self.assertEqual(self.backend.seen_brief["Alpha Brief引用"], normalized["Alpha Brief引用"])
        self.assertFalse((self.runtime.data_root / "CURRENT").exists())

    def test_empty_project_cannot_enter_and_legacy_raw_onboarding_is_rejected(self):
        created = self.flow.create_empty_project(
            self.token,
            workspace_id=self.workspace_id,
            name="匿名空项目",
            category_id="launch",
            expected_workspace_version=0,
        )
        missing = self.client.post(
            f"/api/alpha/projects/{created['project_id']}/enter", headers=self.headers
        )
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()["detail"]["code"], "BRIEF_ARTIFACT_UNAVAILABLE")
        legacy = self.client.post(
            "/api/alpha/projects",
            headers=self.headers,
            json={"brief": {"项目名称": "绕过"}, "confirmed": True},
        )
        self.assertEqual(legacy.status_code, 409)
        self.assertEqual(legacy.json()["detail"]["code"], "ALPHA_PROJECT_ONLY")

    def test_forged_resolver_is_rejected_without_persisting_forged_identity(self):
        project = self.flow.create_empty_project(
            self.token,
            workspace_id=self.workspace_id,
            name="匿名冲突项目",
            category_id="launch",
            expected_workspace_version=0,
        )
        self.alpha_briefs.inputs_root.mkdir()
        first = self.alpha_briefs.inputs_root / "first.txt"
        first.write_text(
            "项目名称: 匿名冲突甲\n脚本类型: 发布会\n目标时长: 60秒\n风格调性: 清晰\n必须出现的内容: 匿名产品",
            encoding="utf-8",
        )
        second = self.alpha_briefs.inputs_root / "second.txt"
        second.write_text("项目名称: 匿名冲突乙", encoding="utf-8")
        documents = [{"path": first.name}, {"path": second.name}]
        self.assertEqual(
            self.client.post(
                f"/api/alpha/projects/{project['project_id']}/brief-inputs/freeze",
                headers=self.headers,
                json={"documents": documents},
            ).status_code,
            200,
        )
        imported = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/brief-imports",
            headers=self.headers,
            json={"documents": documents},
        ).json()
        conflict = imported["conflicts"][0]
        forged = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/brief-conflicts/resolve",
            headers=self.headers,
            json={
                "imported": imported,
                "resolutions": {conflict["conflict_id"]: conflict["fact_ids"][0]},
                "resolved_by": "FORGED_RESOLVED_BY",
            },
        )
        self.assertEqual(forged.status_code, 409)
        self.assertEqual(forged.json()["detail"]["code"], "ALPHA_ACTOR_FORGED")
        self.assertTrue(all(
            "FORGED_RESOLVED_BY" not in path.read_text(encoding="utf-8")
            for path in self.alpha_root.rglob("*.json")
        ))

        resolved = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/brief-conflicts/resolve",
            headers=self.headers,
            json={
                "imported": imported,
                "resolutions": {conflict["conflict_id"]: conflict["fact_ids"][0]},
            },
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["conflicts"][0]["resolved_by"], self.actor_id)

    def test_forged_confirmer_is_rejected_and_real_actor_is_persisted(self):
        project = self.flow.create_empty_project(
            self.token,
            workspace_id=self.workspace_id,
            name="匿名确认项目",
            category_id="launch",
            expected_workspace_version=0,
        )
        self.alpha_briefs.inputs_root.mkdir()
        brief = self.alpha_briefs.inputs_root / "brief.txt"
        brief.write_text(
            "项目名称: 匿名确认项目\n脚本类型: 发布会\n目标时长: 60秒\n风格调性: 清晰\n必须出现的内容: 匿名产品",
            encoding="utf-8",
        )
        documents = [{"path": brief.name}]
        self.assertEqual(
            self.client.post(
                f"/api/alpha/projects/{project['project_id']}/brief-inputs/freeze",
                headers=self.headers,
                json={"documents": documents},
            ).status_code,
            200,
        )
        imported = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/brief-imports",
            headers=self.headers,
            json={"documents": documents},
        ).json()
        forged = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/brief-confirmations",
            headers=self.headers,
            json={
                "imported": imported,
                "confirmed_by": "FORGED_CONFIRMED_BY",
                "expected_project_version": project["project_version"],
            },
        )
        self.assertEqual(forged.status_code, 409)
        self.assertEqual(forged.json()["detail"]["code"], "ALPHA_ACTOR_FORGED")
        self.assertTrue(all(
            "FORGED_CONFIRMED_BY" not in path.read_text(encoding="utf-8")
            for path in self.alpha_root.rglob("*.json")
        ))

        confirmed = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/brief-confirmations",
            headers=self.headers,
            json={
                "imported": imported,
                "expected_project_version": project["project_version"],
            },
        )
        self.assertEqual(confirmed.status_code, 200)
        persisted = self.alpha_briefs.read_confirmed(
            project["project_id"],
            confirmed.json()["confirmed_brief"]["brief_ref"],
            confirmed.json()["confirmed_brief"]["brief_sha256"],
        )
        self.assertEqual(persisted["confirmed_by"], self.actor_id)

    def test_image_delivery_replaces_one_shot_and_reopens_content_addressed_excel(self):
        project_id, version = self._confirmed_project_with_version("匿名图片项目")
        rows = [
            {"镜号": "1", "结构": "开场", "画面描述": "匿名开场"},
            {"镜号": "2", "结构": "收束", "画面描述": "匿名收束"},
        ]
        prepared = self.client.post(
            f"/api/alpha/projects/{project_id}/images/prepare",
            headers=self.headers,
            json={"script_rows": rows},
        )
        self.assertEqual(prepared.status_code, 200)
        self.assertFalse(prepared.json()["calls_models"])
        self.assertEqual(prepared.json()["provider_calls"], 0)
        self.assertTrue(prepared.json()["fixture"])
        candidates = prepared.json()["candidates"]
        self.assertTrue(all(item["image_sha256"] for item in candidates))
        self.assertTrue(all(item["source_sheet"] == "资料" and item["source_row"] == "1" for item in candidates))
        first = self.client.post(
            f"/api/alpha/projects/{project_id}/image-deliveries",
            headers=self.headers,
            json={
                "version_id": version["version_id"], "script_rows": rows,
                "selections": [{"shot_no": item["shot_no"], "candidate_id": item["candidate_id"]} for item in candidates],
            },
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["provider_calls"], 0)
        self.assertTrue(first.json()["fixture"])
        self.assertEqual([item["shot_no"] for item in first.json()["bindings"]], ["1", "2"])
        self.assertEqual(first.json()["bindings"][0]["human_confirmed_by"], self.actor_id)
        self.assertEqual(first.json()["bindings"][0]["image_sha256"], candidates[0]["image_sha256"])
        replaced = self.client.post(
            f"/api/alpha/projects/{project_id}/images/1/replace",
            headers=self.headers,
            json={"script_rows": rows},
        )
        self.assertEqual(replaced.status_code, 200)
        self.assertFalse(replaced.json()["calls_models"])
        self.assertEqual(replaced.json()["provider_calls"], 0)
        self.assertTrue(replaced.json()["fixture"])
        updated = [replaced.json()["candidate"] if item["shot_no"] == "1" else item for item in candidates]
        second = self.client.post(
            f"/api/alpha/projects/{project_id}/image-deliveries",
            headers=self.headers,
            json={
                "version_id": version["version_id"], "script_rows": rows,
                "selections": [{"shot_no": item["shot_no"], "candidate_id": item["candidate_id"]} for item in updated],
            },
        )
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.json()["delivery_sha256"], second.json()["delivery_sha256"])
        download = self.client.get(
            f"/api/alpha/projects/{project_id}/image-deliveries/{second.json()['delivery_sha256']}/workbook",
            headers=self.headers,
        )
        self.assertEqual(download.status_code, 200)
        workbook_path = self.alpha_root / "reopened.xlsx"
        workbook_path.write_bytes(download.content)
        reopened = load_workbook(workbook_path, data_only=True)
        try:
            script = reopened["脚本"]
            self.assertEqual(len(script._images), 2)
            self.assertEqual([str(script.cell(row, 2).value) for row in (2, 3)], ["1", "2"])
            image_column = next(cell.column for cell in script[1] if cell.value == "画面示意")
            self.assertTrue(all(script.cell(row, image_column).value is None for row in (2, 3)))
            headers = {str(cell.value): cell.column for cell in reopened["抽调明细"][1]}
            detail_by_slot = {
                int(reopened["抽调明细"].cell(row, headers["slot_index"]).value): (
                    reopened["抽调明细"].cell(row, headers["采用来源文件"]).value,
                    reopened["抽调明细"].cell(row, headers["采用来源 sheet"]).value,
                    str(reopened["抽调明细"].cell(row, headers["采用来源行"]).value),
                )
                for row in range(2, reopened["抽调明细"].max_row + 1)
            }
            self.assertEqual(set(detail_by_slot), {1, 2})
            self.assertTrue(all(
                document_id and sheet == "资料" and source_row == "1"
                for document_id, sheet, source_row in detail_by_slot.values()
            ))
        finally:
            reopened.close()

    def test_image_delivery_rejects_nonexistent_pdf_alpha_source_and_mutated_candidate(self):
        project_id, version = self._confirmed_project_with_version("匿名图片防篡改项目")
        rows = [{"镜号": "1", "结构": "开场", "画面描述": "匿名开场"}]
        candidate = self.images.replace_image(
            self.token, project_id=project_id, shot_no="1", script_rows=rows
        )["candidate"]
        candidate_path = Path(candidate["image_path"])
        candidate_path.write_bytes(b"not the frozen image")
        mutated = self.client.post(
            f"/api/alpha/projects/{project_id}/image-deliveries", headers=self.headers,
            json={"version_id": version["version_id"], "script_rows": rows,
                  "selections": [{"shot_no": "1", "candidate_id": candidate["candidate_id"]}]},
        )
        self.assertEqual(mutated.status_code, 409)
        self.assertEqual(mutated.json()["detail"]["code"], "ALPHA_IMAGE_CANDIDATE_MISMATCH")
        self.assertFalse((self.alpha_root / "image-deliveries" / project_id).exists())
        self.assertFalse(list(self.alpha_root.glob("alpha-image-delivery-*.xlsx")))

        prepared = self.images.prepare_images(self.token, project_id=project_id, script_rows=rows)
        candidate = prepared["candidates"][0]
        path = self.alpha_root / "image-candidates" / f"{project_id}.json"
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["items"][-1].update({"source_document_id": "alpha_doc_pdf", "source_sheet": "Alpha", "source_row": "1"})
        path.write_text(json.dumps(stored), encoding="utf-8")
        forged_source = self.client.post(
            f"/api/alpha/projects/{project_id}/image-deliveries", headers=self.headers,
            json={"version_id": version["version_id"], "script_rows": rows,
                  "selections": [{"shot_no": "1", "candidate_id": candidate["candidate_id"]}]},
        )
        self.assertEqual(forged_source.status_code, 409)
        self.assertEqual(forged_source.json()["detail"]["code"], "ALPHA_SOURCE_EVIDENCE_UNAVAILABLE")
        self.assertFalse((self.alpha_root / "image-deliveries" / project_id).exists())
        self.assertFalse(list(self.alpha_root.glob("alpha-image-delivery-*.xlsx")))

    def test_service_restart_recovers_persistent_project_brief_session_events_version_review_and_delivery(self):
        project_id, version = self._confirmed_project_with_version("匿名重启项目")
        session = self.console.create_session(self.token, project_id=project_id)["session"]
        problem = self.console.confirm_problem_shot(
            self.token, project_id=project_id, agent_session_id=session["agent_session_id"],
            shot_no="1", decision="accepted_with_note", evidence_ref="artifact://review/fixture/shot-1",
            expected_project_version=version["project_version"],
        )
        review = self.console.confirm_whole_script(
            self.token, project_id=project_id, version_id=version["version_id"],
            evidence_ref="artifact://review/fixture/whole", expected_project_version=problem["project_version_after"],
        )
        rows = [{"镜号": "1", "结构": "开场", "画面描述": "匿名重启画面"}]
        candidate = self.images.prepare_images(self.token, project_id=project_id, script_rows=rows)["candidates"][0]
        delivery = self.images.create_delivery(
            self.token, project_id=project_id, version_id=version["version_id"], script_rows=rows,
            selections=[{"shot_no": "1", "candidate_id": candidate["candidate_id"]}],
        )

        restarted = self._rebuild_console()
        dashboard = restarted.project_dashboard(
            self.token, project_id=project_id, agent_session_id=session["agent_session_id"]
        )
        self.assertEqual(
            restarted._readonly().services.projects.get_current_brief_snapshot(
                self.token, project_id
            )["brief_sha256"],
            self.agent.services.projects.get_current_brief_snapshot(
                self.token, project_id
            )["brief_sha256"],
        )
        self.assertEqual(
            restarted._readonly().services.tasks.get_agent_session(
                self.token, session["agent_session_id"]
            )["project_id"],
            project_id,
        )
        self.assertIn(
            version["version_id"],
            {
                item["version_id"]
                for item in restarted._readonly().services.projects.list_project_versions(
                    self.token, project_id
                )
            },
        )
        reviews = restarted._readonly().services.versions.list_reviews(self.token, version["version_id"])
        self.assertEqual(reviews[-1]["review_id"], review["review"]["review_id"])
        workbook = restarted.image_workbook(self.token, project_id=project_id, delivery_sha256=delivery["delivery_sha256"])
        self.assertTrue(workbook.is_file())
        events = restarted._readonly().services.tasks.list_task_events(self.token, problem["task"]["task_id"])
        self.assertEqual(events[-1]["status"], "completed")

    def _rebuild_console(self) -> AlphaConsole:
        """Destroy/recreate service objects over the unchanged isolated roots."""
        store = WorkspaceStore(self.runtime.data_root / "workspace.sqlite3")
        identity = LocalIdentityAdapter(store)
        readonly = ReadonlyAgentService(ReadonlyAgentServices(
            store=store, identity=identity, projects=ProjectService(store), tasks=TaskService(store),
            versions=VersionService(store), artifacts=RunArtifactResolver(self.runtime.runs_root),
        ))
        briefs = BriefArtifactStore(self.runtime.data_root)
        agent = ExecutableAgentService(ExecutableAgentServices(
            readonly=readonly, projects=readonly.services.projects, tasks=readonly.services.tasks,
            confirmations=ActionConfirmationService(store, b"i" * 32), idempotency=ToolIdempotencyService(store),
            brief_store=briefs, backend=_SnapshotBackend(briefs),
        ))
        flow = AlphaProjectFlow(projects=readonly.services.projects, identity=identity, alpha_briefs=self.alpha_briefs, normalized_briefs=briefs)
        images = AlphaImageFlow(projects=readonly.services.projects, identity=identity, alpha_briefs=self.alpha_briefs)
        return AlphaConsole(self.runtime, agent, verified_brief_reader=lambda *_: {"unused": True}, project_flow=flow, image_flow=images)

    def _confirmed_project_with_version(self, name: str) -> tuple[str, dict]:
        created = self.flow.create_empty_project(
            self.token, workspace_id=self.workspace_id, name=name,
            category_id="launch", expected_workspace_version=0,
        )
        self.alpha_briefs.inputs_root.mkdir(exist_ok=True)
        brief = self.alpha_briefs.inputs_root / f"{created['project_id']}.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "资料"
        for row, value in enumerate((
            f"项目名称: {name}", "脚本类型: 发布会", "目标时长: 60秒",
            "风格调性: 清晰", "必须出现的内容: 匿名产品",
        ), start=1):
            sheet.cell(row, 1).value = value
        workbook.save(brief)
        documents = [{"path": brief.name}]
        frozen = self.flow.freeze_documents(self.token, project_id=created["project_id"], documents=documents)
        self.assertTrue(frozen["inputs_sha256"])
        imported = self.flow.import_documents(self.token, project_id=created["project_id"], documents=documents)
        self.flow.confirm_and_bind(
            self.token, project_id=created["project_id"], imported=imported,
            expected_project_version=created["project_version"],
        )
        run_id = "alpha-run-image-0001"
        run_dir = self.runtime.runs_root / "anonymous" / "runs" / run_id
        run_dir.mkdir(parents=True)
        report = run_dir / "run_report.json"
        report.write_text(json.dumps({"machine_audit_status": "pass", "human_review_status": "pending", "blocking_items": [{"shot_no": "1", "code": "image_replace", "message": "换图"}]}), encoding="utf-8")
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(json.dumps({"run_id": run_id, "status": "waiting_human_review", "stages": {}, "artifacts": {"run_report": {"path": str(report), "sha256": _sha(report), "size_bytes": report.stat().st_size}}}), encoding="utf-8")
        bound = self.agent.services.projects.bind_run(
            self.token, project_id=created["project_id"], run_id=run_id, source_kind="native",
            manifest_ref=f"artifact://run/{run_id}/manifest", manifest_sha256=_sha(manifest), expected_project_version=1,
        )
        version = self.agent.services.projects.create_project_version(
            self.token, project_id=created["project_id"], run_id=run_id,
            content_sha256=hashlib.sha256(b"anonymous image version").hexdigest(),
            artifact_ref=f"artifact://run/{run_id}/workbook", expected_project_version=bound["project_version"],
        )
        return created["project_id"], version


if __name__ == "__main__":
    unittest.main()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
