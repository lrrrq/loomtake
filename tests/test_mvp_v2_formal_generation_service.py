from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from PIL import Image as PILImage

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.generation_stages import _confirmed_brief_evidence
from mvp_v2.excel_io import ADJACENT_REVIEW_SHEET_NAME, write_template_output_workbook
from mvp_v2.formal_brief_domain import ArtifactPayload, SourceManifest
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_generation_service import (
    FormalGenerationBlocked,
    FormalGenerationService,
    FormalGenerationServices,
    FormalTextProviderPolicy,
    FormalVisionProviderPolicy,
)
from mvp_v2.image_understanding import ProjectImageVisionResult
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.knowledge_service import KnowledgeService
from mvp_v2.asset_package import (
    asset_package_source_record, bundle_asset_package_v3, create_asset_package,
)
from mvp_v2.package_registry import build_package_record, PackageRegistryError
from mvp_v2.project_service import ProjectService
from mvp_v2.project_image_authority import (
    ArtifactEvidence, ManualProjectImageAnalysisCommandService, ProjectImage,
    ProjectImageAnalysis, ProjectImageBusinessSummary, ProjectImagePlanningReadiness,
    ProjectImagePoolService,
)
from mvp_v2.task_service import TaskService
from mvp_v2.run_contract import sha256_json
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore, new_id, utc_now
from tests.test_mvp_v2 import full_launch_template_rows, write_launch_library
from tests.test_mvp_v2_project_image_authority import _representative_uploads, _submission
from mvp_v2.ai_spec import refresh_ai_spec


class FormalGenerationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mplus-formal-generation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_root = self.root / "source"
        self.source_root.mkdir()
        workbook = self.source_root / "M+发布会类通用脚本库.xlsx"
        write_launch_library(workbook, full_launch_template_rows(), [])
        source = load_workbook(workbook)
        for row in range(7, 25):
            source["模版"].cell(row, 10, "现场氛围与音乐推进")
        source.save(workbook)
        source.close()
        refresh_ai_spec(workbook)
        self.store = WorkspaceStore(self.root / "runtime" / "workspace.sqlite3")
        owner = LocalIdentityAdapter(self.store).bootstrap_owner(
            workspace_name="匿名正式工作区", display_name="匿名 owner", confirmed=True,
            token="formal-generation-owner",
        )
        self.token, self.workspace_id = owner["auth_token"], owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.knowledge = KnowledgeService(self.store)
        self.project = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名发布会",
            category_id="launch", expected_workspace_version=0,
        )
        self.briefs = FormalBriefWorkspaceService(self.store, artifact_root=self.root / "runtime")
        self.confirmed = self._confirmed_brief()

    def _confirmed_brief(
        self,
        *,
        expected_project_version=0,
        required_content: str = "核心宣讲与仪式",
        forbidden_content: str = "不得虚构事实",
        event_titles: list[str] | None = None,
        omit_fields: tuple[str, ...] = (),
        target_duration: str = "60s",
    ):
        source_bytes = f"anonymous formal brief {expected_project_version}".encode("utf-8")
        digest = sha256(source_bytes).hexdigest()
        document_id = f"anonymous_doc_{expected_project_version}"
        stored = (
            self.root / "runtime" / "artifacts" / "formal-inputs" / "files"
            / digest[:2] / f"{digest}.txt"
        )
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(source_bytes)
        manifest = SourceManifest.from_mapping({
            "manifest": {"ref": "artifact://source/manifest", "sha256": digest},
            "documents": [{"source_document_id": document_id, "relative_path": f"uploads/{document_id}.txt", "content_sha256": digest, "critical": True}],
        })
        brief = {
            "项目名称": "匿名发布会", "脚本类型": "发布会", "视频时长": target_duration,
            "品牌或客户": "匿名品牌", "风格调性": "克制、真实",
            "必须出现的内容": required_content,
            "禁忌或不能出现的内容": forbidden_content,
            "planning_events": [
                {"title": title, "evidence": f"Brief明确要求{title}"}
                for title in (
                    event_titles
                    or ["开场造势", "核心宣讲与仪式", "收束"]
                )
            ],
        }
        for field in omit_fields:
            brief.pop(field, None)
        evidence = f"artifact://formal-evidence/{document_id}/page/1"
        extracted = {
            "project_id": self.project["project_id"],
            "source_documents": [{"document_id": document_id, "project_id": self.project["project_id"], "filename": f"{document_id}.txt", "mime_type": "text/plain", "size_bytes": len(source_bytes), "sha256": digest, "critical": True}],
            "extractions": [{"document_id": document_id, "status": "succeeded", "critical": True, "error": None, "locations": [{"evidence_ref": evidence, "location": {"page": 1}, "text": "匿名正式 Brief"}]}],
            "images": [],
            "facts": [{"fact_id": f"fact_{self.project['project_id']}_{expected_project_version}_{index}", "field": key, "value": value, "confidence": "direct", "evidence_refs": [evidence]} for index, (key, value) in enumerate(brief.items(), start=1)],
            "conflicts": [],
        }
        pending = self.briefs.import_frozen(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            source_manifest=manifest,
            source_artifacts={document_id: ArtifactPayload(f"artifact://formal-input/{digest}", digest)},
            extracted=extracted, expected_project_version=expected_project_version,
        )
        return self.briefs.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            brief_version_id=pending.brief_version_id, confirmed_at="2026-08-13T00:00:00Z",
            expected_project_version=expected_project_version + 1,
        )

    def _service(self, policy=None, *, vision=None, store=None, briefs=None,
                 knowledge=None, asset_package_resolver=None):
        store = store or self.store
        return FormalGenerationService(
            FormalGenerationServices(
                briefs=briefs or self.briefs, projects=ProjectService(store),
                tasks=TaskService(store), artifacts=RunArtifactResolver(self.root / "runs"),
                images=ProjectImagePoolService(store, data_root=self.root / "runtime"),
                knowledge=knowledge or KnowledgeService(store),
            ),
            runs_root=self.root / "runs", source_root=self.source_root,
            asset_package_resolver=asset_package_resolver,
            provider_policy=policy, vision_provider_policy=vision,
        )

    def _approve_project_rule(self, statement: str) -> dict:
        state = self.knowledge.get_state(
            self.token, self.workspace_id, project_id=self.project["project_id"],
        )
        candidate = self.knowledge.create_candidate(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            scope="project",
            target_id=self.project["project_id"],
            statement=statement,
            evidence_refs=["artifact://anonymous-knowledge/evidence"],
            expected_knowledge_version=state["knowledge_version"],
        )
        return self.knowledge.approve_candidate(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            candidate_id=candidate["candidate_id"],
            level="project",
            review_evidence="匿名管理员已核对",
            expected_knowledge_version=candidate["knowledge_version"],
        )

    def _confirm_adapter_brief(self) -> object:
        """Use T01's stream-only adapter, never a hand-built formal payload."""
        adapter = FormalBriefInputAdapter(self.briefs, data_root=self.root / "runtime")
        adapter.import_uploads(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            expected_project_version=2,
            _replace_document_ids={"anonymous_doc_0"},
            uploads=[UploadedDocument(
                "anonymous-canonical-brief.txt",
                BytesIO("\n".join((
                    "项目名称: 匿名发布会",
                    "脚本类型: 发布会",
                    "品牌或客户: 匿名品牌",
                    "目标时长: 60秒",
                    "风格调性: 克制、真实",
                    "视频目标: 产品演示",
                    "使用场景: 线下新品发布现场",
                    "必须出现的内容: 产品主视觉",
                    "关键事件与流程: 开场、产品主视觉展示、产品演示、品牌收束、行动号召",
                    "禁止或不能出现的内容: 不得虚构事实",
                    "参考图片: 已确认的现场主视觉参考",
                )).encode("utf-8")),
            )],
        )
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT brief_version_id FROM brief_versions WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                (self.project["project_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        return self.briefs.confirm(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            brief_version_id=row["brief_version_id"],
            confirmed_at="2026-08-14T00:00:00Z",
            expected_project_version=3,
        )

    def _confirm_adapter_conflict(self):
        adapter = FormalBriefInputAdapter(self.briefs, data_root=self.root / "runtime")
        common = "\n".join((
            "脚本类型: 发布会", "品牌或客户: 匿名品牌", "目标时长: 60秒",
            "风格调性: 克制、真实", "视频目标: 产品演示",
            "使用场景: 线下新品发布现场", "必须出现的内容: 产品主视觉",
            "关键事件与流程: 开场、产品主视觉展示、产品演示、品牌收束",
            "禁止或不能出现的内容: 不得虚构事实",
        ))
        pending = adapter.import_uploads(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=2,
            _replace_document_ids={"anonymous_doc_0"},
            uploads=[
                UploadedDocument("anonymous-a.txt", BytesIO(("项目名称: 匿名甲\n" + common).encode("utf-8"))),
                UploadedDocument("anonymous-b.txt", BytesIO(("项目名称: 匿名乙\n" + common).encode("utf-8"))),
            ],
        )
        conflict = pending["brief"]["conflicts"][0]
        selected = next(
            item for item in conflict["choices"]
            if json.loads(item["value"]) == "匿名乙"
        )
        resolved = self.briefs.resolve_conflict(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            brief_version_id=pending["brief_key"],
            conflict_id=conflict["conflict_key"],
            selected_fact_id=selected["fact_key"], expected_project_version=3,
        )
        confirmed = self.briefs.confirm(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            brief_version_id=resolved.brief_version_id,
            confirmed_at="2026-08-14T00:00:00Z", expected_project_version=4,
        )
        return confirmed, resolved

    def _confirm_adapter_two_conflicts(self):
        adapter = FormalBriefInputAdapter(self.briefs, data_root=self.root / "runtime")
        common = "\n".join((
            "脚本类型: 发布会", "目标时长: 60秒", "风格调性: 克制、真实",
            "视频目标: 产品演示", "使用场景: 线下新品发布现场",
            "必须出现的内容: 产品主视觉",
            "关键事件与流程: 开场、产品主视觉展示、产品演示、品牌收束",
            "禁止或不能出现的内容: 不得虚构事实",
        ))
        pending = adapter.import_uploads(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=2,
            _replace_document_ids={"anonymous_doc_0"},
            uploads=[
                UploadedDocument(
                    "anonymous-a.txt",
                    BytesIO((
                        "项目名称: 匿名甲\n品牌或客户: 品牌甲\n" + common
                    ).encode("utf-8")),
                ),
                UploadedDocument(
                    "anonymous-b.txt",
                    BytesIO((
                        "项目名称: 匿名乙\n品牌或客户: 品牌乙\n" + common
                    ).encode("utf-8")),
                ),
            ],
        )

        def resolve(version, *, field_path, selected_value, expected_lock):
            if isinstance(version, dict):
                conflict = next(
                    item for item in version["brief"]["conflicts"]
                    if item["resolution_status"] == "unresolved"
                    and any(
                        json.loads(choice["value"]) == selected_value
                        for choice in item["choices"]
                    )
                )
                selected_fact_id = next(
                    choice["fact_key"] for choice in conflict["choices"]
                    if json.loads(choice["value"]) == selected_value
                )
                brief_version_id = version["brief_key"]
                conflict_id = conflict["conflict_key"]
            else:
                conflict = next(
                    item for item in version.payload_data["conflicts"]
                    if item["field_path"] == field_path
                    and item["resolution_status"] == "unresolved"
                )
                selected_fact_id = next(
                    fact["fact_id"] for fact in version.payload_data["facts"]
                    if fact["fact_id"] in conflict["fact_ids"]
                    and json.loads(fact["value_json"]) == selected_value
                )
                brief_version_id = version.brief_version_id
                conflict_id = conflict["conflict_id"]
            return self.briefs.resolve_conflict(
                self.token, workspace_id=self.workspace_id,
                project_id=self.project["project_id"],
                brief_version_id=brief_version_id,
                conflict_id=conflict_id,
                selected_fact_id=selected_fact_id,
                expected_project_version=expected_lock,
            )

        first = resolve(
            pending, field_path="项目名称", selected_value="匿名乙", expected_lock=3,
        )
        final = resolve(
            first, field_path="品牌或客户", selected_value="品牌乙", expected_lock=4,
        )
        confirmed = self.briefs.confirm(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            brief_version_id=final.brief_version_id,
            confirmed_at="2026-08-14T00:00:00Z", expected_project_version=5,
        )
        return confirmed, final

    def _table_count(self, table: str) -> int:
        with self.store.connection() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _provider(self, _name, calls):
        def invoke(prompt, model, max_tokens):
            if "\nINPUT:" in prompt:
                # This writer fixture covers drafting. Record the separate
                # planning attempt and exercise its explicit fallback.
                self.planning_calls = getattr(self, "planning_calls", [])
                self.planning_calls.append((prompt, model, max_tokens))
                return "{}"
            calls.append((_name, model, max_tokens))
            source_text = prompt.split("\nDATA:", 1)[1].split("\nDRAFT:", 1)[0]
            source = json.loads(source_text)
            shots = []
            for item in source["shots"]:
                visual = str(item.get("basis") or "已确认活动现场").strip("。；")
                visual = visual.replace(
                    "智美万象主题字样与品牌logo", "匿名发布会主题字样与匿名品牌标志"
                ).replace("品牌logo", "匿名品牌标志")
                for required in item.get("required_brief_content") or []:
                    if required not in visual:
                        visual += f"，呈现{required}"
                shots.append({
                    "shot_no": str(item["shot_no"]),
                    "visual_description": visual,
                    "sound": "现场氛围与音乐推进",
                    "voiceover": "",
                    "subtitle": "",
                    "pending_confirmations": [],
                })
            return json.dumps({
                "schema_version": "mplus-script-draft-response/v1",
                "overall_summary": "围绕确认 Brief 形成可执行的现场叙事。",
                "shots": shots,
            }, ensure_ascii=False)
        return invoke

    def test_prepare_plan_requires_explicit_image_strategy_after_authority_checks(self):
        service = self._service()
        with self.assertRaises(FormalGenerationBlocked) as caught:
            service.prepare_plan(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project["project_id"],
                expected_project_version=2,
            )
        self.assertEqual(caught.exception.code, "IMAGE_STRATEGY_CONFIRMATION_REQUIRED")

    def test_replan_creates_child_version_and_unsaved_script_draft_blocks_before_provider(self):
        calls = []
        policy = FormalTextProviderPolicy(
            writer=self._provider("writer", calls), editor=self._provider("editor", calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        )
        service = self._service(policy)
        first_plan = service.prepare_plan(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        first = service.confirm_plan_and_generate(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            plan_id=first_plan["plan_id"],
            expected_project_version=self.projects.get_project(
                self.token, self.project["project_id"],
            )["project_version"],
        )
        current = self.projects.get_project(self.token, self.project["project_id"])
        second_plan = service.prepare_plan(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            expected_project_version=current["project_version"],
            generation_mode="text_only_draft",
        )
        second = service.confirm_plan_and_generate(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            plan_id=second_plan["plan_id"],
            expected_project_version=self.projects.get_project(
                self.token, self.project["project_id"],
            )["project_version"],
        )
        versions = self.projects.list_project_versions(self.token, self.project["project_id"])
        self.assertEqual(versions[-1]["version_id"], second["version_id"])
        self.assertEqual(versions[-1]["parent_version_id"], first["version_id"])

        with self.store.transaction() as connection:
            base_state_id = new_id("review_state")
            changed_state_id = new_id("review_state")
            draft_id = new_id("draft")
            actor_id = connection.execute(
                "SELECT actor_id FROM actors ORDER BY created_at LIMIT 1",
            ).fetchone()[0]
            rows = [{"shot_no": "1", "fields": {"字幕": ""}, "source_refs": ["来源"]}]
            changed_rows = [{"shot_no": "1", "fields": {"字幕": "未保存"}, "source_refs": ["来源"]}]
            connection.execute(
                "INSERT INTO version_review_drafts VALUES (?, ?, ?, ?, 'open', 1, ?, ?)",
                (
                    draft_id, self.workspace_id, self.project["project_id"], second["version_id"],
                    actor_id, utc_now(),
                ),
            )
            connection.execute(
                "INSERT INTO version_review_draft_states VALUES (?, ?, NULL, ?, ?, ?, ?)",
                (
                    base_state_id, draft_id, json.dumps(rows, ensure_ascii=False),
                    sha256_json(rows), actor_id, utc_now(),
                ),
            )
            connection.execute(
                "INSERT INTO version_review_draft_states VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    changed_state_id, draft_id, base_state_id,
                    json.dumps(changed_rows, ensure_ascii=False), sha256_json(changed_rows),
                    actor_id, utc_now(),
                ),
            )
            connection.execute(
                "INSERT INTO version_review_draft_heads VALUES (?, ?, ?, '[]', ?)",
                (draft_id, changed_state_id, json.dumps([base_state_id]), utc_now()),
            )

        before_calls = len(calls)
        current = self.projects.get_project(self.token, self.project["project_id"])
        with self.assertRaises(FormalGenerationBlocked) as blocked:
            service.prepare_plan(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project["project_id"],
                expected_project_version=current["project_version"],
                generation_mode="text_only_draft",
            )
        self.assertEqual(blocked.exception.code, "SCRIPT_DRAFT_NOT_SAVED")
        self.assertEqual(len(calls), before_calls)

    def test_mixed_image_statuses_keep_formal_planning_context_on_pass_images(self):
        """Attention images are itemwise excluded; verified pass images stay usable."""
        def image(index: int, status: str, *, summary: bool = True) -> ProjectImage:
            analysis = ProjectImageAnalysis(
                f"analysis-{index}", status,
                ArtifactEvidence(
                    f"artifact://project-image-analysis/{index:064d}",
                    f"{index:064d}",
                ),
                "anonymous-actor", "2026-08-20T00:00:00Z",
            )
            business_summary = ProjectImageBusinessSummary(
                caption=f"青屿明星见面日镜头{index}", tags=("明星活动",),
                subjects=("嘉宾",), scene="见面会现场", purpose="脚本候选",
                quality="清晰", brand="青屿", evidence_status="confirmed",
            ) if summary else None
            return ProjectImage(
                f"image-{index}", f"{index:064d}", (), analysis, business_summary,
            )

        readiness = ProjectImagePlanningReadiness(
            self.workspace_id, self.project["project_id"],
            self.confirmed.brief_version_id, self.confirmed.brief_snapshot_id,
            6, 2, 1, 1, 1, 1,
            tuple(image(index, status) for index, status in enumerate(
                ("pass", "pass", "pending", "low_confidence", "blocked", "failed"),
                start=1,
            )),
        )
        service = self._service()
        service._require_images_ready(readiness)
        evidence = service._image_event_evidence(readiness)
        self.assertEqual(evidence["total"], 6)
        self.assertEqual(evidence["passed"], 2)
        self.assertEqual([item["image_id"] for item in evidence["images"]], ["image-1", "image-2"])
        drifted = replace(readiness, passed=1, images=readiness.images[1:])
        with patch.object(service, "_resolve_images", return_value=drifted):
            with self.assertRaisesRegex(FormalGenerationBlocked, "图片分析绑定"):
                service._require_same_images(
                    self.token, workspace_id=self.workspace_id,
                    project_id=self.project["project_id"], paired=self.confirmed,
                    expected_project_version=2, expected=evidence,
                    generation_mode="image_backed",
                )

    def test_zero_pass_images_require_explicit_text_only_mode(self):
        readiness = ProjectImagePlanningReadiness(
            self.workspace_id, self.project["project_id"],
            self.confirmed.brief_version_id, self.confirmed.brief_snapshot_id,
            2, 0, 1, 0, 0, 1, (),
        )
        service = self._service()
        service._require_images_ready(readiness, generation_mode="text_only_draft")
        with self.assertRaisesRegex(FormalGenerationBlocked, "没有可用于正式规划"):
            service._require_images_ready(readiness, generation_mode="image_backed")

    def test_adapter_canonical_fields_drive_p00_and_keep_references_cstd_only(self):
        """T01 canonical storage names are projected, never rewritten in storage."""
        confirmed = self._confirm_adapter_brief()
        calls = []
        policy = FormalTextProviderPolicy(
            writer=self._provider("writer", calls), editor=self._provider("editor", calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        )
        service = self._service(policy)
        (self.source_root / "poison-legacy-source-root.png").write_bytes(b"not-a-formal-image")
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=4,
            generation_mode="text_only_draft",
        )
        self.assertEqual(preview["shot_count"], 17)
        self.assertEqual(len(preview["events"]), 5)
        brief_shots = [
            shot for shot in preview["shots"]
            if any(not str(event.get("event_id") or "").startswith("template_slot_") for event in shot["events"])
        ]
        placeholder_shots = [shot for shot in preview["shots"] if shot not in brief_shots]
        self.assertTrue(brief_shots)
        self.assertTrue(all(shot["source"]["file"] == "anonymous-canonical-brief.txt" for shot in brief_shots))
        self.assertTrue(placeholder_shots)
        self.assertTrue(all(shot["structure_source"] == "structural_required" for shot in placeholder_shots))
        self.assertEqual(calls, [])
        self.expected_shot_count = preview["shot_count"]
        details = service.services.tasks.list_task_events(self.token, preview["plan_id"])[-1]["details"]
        run_id = service._run_id_from_ref(details["plan_ref"])
        plan_raw = json.loads(
            service.services.artifacts.resolve_stage_artifact(run_id, "script_plan").path.read_text(encoding="utf-8")
        )
        manifest = service.services.artifacts.read_manifest(run_id)
        self.assertEqual(manifest["stages"]["media_manifest"]["status"], "succeeded")
        self.assertEqual(manifest["stages"]["visual_analysis"]["status"], "succeeded")
        self.assertEqual(
            Path(manifest["stages"]["media_manifest"]["output_refs"][0]).read_text(encoding="utf-8"),
            "",
        )
        self.assertEqual(plan_raw["constraints"]["project_image_context"]["total"], 0)
        self.assertEqual(plan_raw["brief"]["视频时长"], "60秒")
        self.assertEqual(plan_raw["brief"]["禁忌或不能出现的内容"], "不得虚构事实")
        self.assertEqual(plan_raw["brief"]["事件流程"], "开场、产品主视觉展示、产品演示、品牌收束、行动号召")
        self.assertEqual(
            [item["title"] for item in plan_raw["brief"]["planning_events"]],
            ["开场", "产品主视觉展示", "产品演示", "品牌收束", "行动号召"],
        )
        self.assertTrue(all(item["fact_status"] == "confirmed" for item in plan_raw["brief"]["planning_events"]))
        self.assertEqual(plan_raw["brief"]["视频目标"], "产品演示")
        self.assertEqual(plan_raw["brief"]["使用场景"], "线下新品发布现场")
        for key in ("script_rows", "detail_rows", "decisions", "template_slots", "shot_plans"):
            self.assertEqual(len(plan_raw[key]), preview["shot_count"])
        self.assertNotIn("参考图片", plan_raw["brief"])
        self.assertNotIn("参考图片", json.dumps(plan_raw["events"], ensure_ascii=False))
        provenance = plan_raw["confirmed_brief_provenance"]["facts"]
        fields = {item["field_path"]: item for item in provenance}
        self.assertEqual(fields["目标时长"]["planner_field"], "视频时长")
        self.assertEqual(fields["禁止或不能出现的内容"]["planner_field"], "禁忌或不能出现的内容")
        self.assertEqual(fields["关键事件与流程"]["planner_field"], "事件流程")
        self.assertEqual(fields["关键事件与流程"]["event_projection"]["event_count"], 5)
        self.assertEqual(fields["参考图片"]["consumption"], "creative_standard_reference_only")
        self.assertTrue(all(item["evidence_refs"] for item in provenance))
        plan_facts = {item["field"]: item["value"] for item in plan_raw["brief_facts"]}
        self.assertEqual(plan_facts["视频目标"], "产品演示")
        self.assertEqual(plan_facts["使用场景"], "线下新品发布现场")
        paired = self.briefs.resolve_current_paired(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
        )
        standard = self.briefs.creative_standard_resolver().resolve(paired.creative_standard)
        self.assertEqual(standard["references"][0]["value"], "已确认的现场主视觉参考")
        self.assertEqual(standard["avoid_expressions"][0]["value"], "不得虚构事实")
        result = service.confirm_plan_and_generate(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            plan_id=preview["plan_id"], expected_project_version=5,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls), 2)
        completed_details = service.services.tasks.list_task_events(
            self.token, preview["plan_id"]
        )[-1]["details"]
        child_manifest = service.services.artifacts.read_manifest(completed_details["run_id"])
        self.assertEqual(child_manifest["stages"]["media_manifest"]["attempt"], manifest["stages"]["media_manifest"]["attempt"])
        self.assertEqual(child_manifest["stages"]["visual_analysis"]["attempt"], manifest["stages"]["visual_analysis"]["attempt"])
        self.assertTrue(child_manifest["stages"]["media_manifest"].get("reused_from_run"))
        self.assertTrue(child_manifest["stages"]["visual_analysis"].get("reused_from_run"))
        draft_raw = json.loads(
            service.services.artifacts.resolve_stage_artifact(
                completed_details["run_id"],
                "script_draft",
            ).path.read_text(encoding="utf-8")
        )
        self.assertEqual(draft_raw["brief"]["视频目标"], "产品演示")
        self.assertEqual(draft_raw["brief"]["使用场景"], "线下新品发布现场")
        workbook = service.services.artifacts.resolve_artifact(completed_details["run_id"], "workbook")
        rendered = load_workbook(workbook.path, data_only=True)
        self.addCleanup(rendered.close)
        self.assertEqual(rendered[ADJACENT_REVIEW_SHEET_NAME].max_row - 1, preview["shot_count"] - 1)

    def test_confirmed_plan_does_not_create_version_when_machine_audit_fails(self):
        self._confirm_adapter_brief()
        calls = []
        policy = FormalTextProviderPolicy(
            writer=self._provider("writer", calls), editor=self._provider("editor", calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        )
        service = self._service(policy)
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=4,
            generation_mode="text_only_draft",
        )

        with patch(
            "mvp_v2.formal_generation_service.run_generation_once",
            return_value=SimpleNamespace(audit={
                "status": "fail", "blocking_items": ["required_content_missing"],
            }),
        ):
            with self.assertRaises(FormalGenerationBlocked) as raised:
                service.confirm_plan_and_generate(
                    self.token, workspace_id=self.workspace_id,
                    project_id=self.project["project_id"], plan_id=preview["plan_id"],
                    expected_project_version=5,
                )

        self.assertEqual(raised.exception.code, "FORMAL_GENERATION_AUDIT_FAILED")
        self.assertIsNone(
            service.services.projects.get_project(
                self.token, self.project["project_id"]
            )["current_version_id"]
        )

    def test_resolved_conflict_consumes_only_human_choice_with_resolution_provenance(self):
        confirmed, resolved = self._confirm_adapter_conflict()
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=5,
            generation_mode="text_only_draft",
        )
        self.assertEqual(preview["shot_count"], 17)
        details = service.services.tasks.list_task_events(
            self.token, preview["plan_id"]
        )[-1]["details"]
        run_id = service._run_id_from_ref(details["plan_ref"])
        plan = json.loads(
            service.services.artifacts.resolve_stage_artifact(
                run_id, "script_plan"
            ).path.read_text(encoding="utf-8")
        )
        self.assertEqual(plan["brief"]["项目名称"], "匿名乙")
        for key in ("script_rows", "detail_rows", "decisions", "template_slots", "shot_plans"):
            self.assertEqual(len(plan[key]), preview["shot_count"])
        conflict = resolved.payload_data["conflicts"][0]
        by_fact = {
            item["fact_id"]: item
            for item in plan["confirmed_brief_provenance"]["facts"]
        }
        selected = by_fact[conflict["resolved_fact_id"]]
        self.assertEqual(
            selected["conflict_resolution"]["resolution_evidence"],
            conflict["resolution_evidence"],
        )
        self.assertEqual(
            selected["conflict_resolution"]["resolved_by_actor_id"],
            conflict["resolved_by_actor_id"],
        )
        self.assertEqual(
            selected["conflict_resolution"]["resolved_at"], conflict["resolved_at"]
        )
        rejected_ids = set(conflict["fact_ids"]) - {conflict["resolved_fact_id"]}
        self.assertEqual(
            {by_fact[item]["consumption"] for item in rejected_ids},
            {"conflict_candidate_not_selected"},
        )
        output = self.root / "resolved-conflict-plan.xlsx"
        write_template_output_workbook(
            output, plan["script_rows"], plan["detail_rows"], []
        )
        workbook = load_workbook(output, data_only=True)
        self.addCleanup(workbook.close)
        self.assertEqual(
            workbook[ADJACENT_REVIEW_SHEET_NAME].max_row - 1,
            preview["shot_count"] - 1,
        )
        self.assertEqual(self._table_count("project_versions"), 0)

    def test_resolution_evidence_bytes_drift_blocks_before_task_or_provider(self):
        _confirmed, resolved = self._confirm_adapter_conflict()
        evidence = resolved.payload_data["conflicts"][0]["resolution_evidence"]
        target = (
            self.root / "runtime" / "formal-brief-payloads"
            / evidence["sha256"][:2] / f"{evidence['sha256']}.json"
        )
        target.write_bytes(target.read_bytes() + b"tampered")
        before = self._table_count("tasks")
        with self.assertRaisesRegex(FormalGenerationBlocked, "漂移|损坏|SHA-256"):
            self._service().prepare_plan(
                self.token, workspace_id=self.workspace_id,
                project_id=self.project["project_id"], expected_project_version=5,
                generation_mode="text_only_draft",
            )
        self.assertEqual(self._table_count("tasks"), before)
        self.assertEqual(self._table_count("project_versions"), 0)

    def test_two_conflicts_resolve_sequentially_and_only_selected_facts_reach_p00(self):
        _confirmed, resolved = self._confirm_adapter_two_conflicts()
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=6,
            generation_mode="text_only_draft",
        )
        self.assertEqual(preview["shot_count"], 17)
        details = service.services.tasks.list_task_events(
            self.token, preview["plan_id"]
        )[-1]["details"]
        plan = json.loads(
            service.services.artifacts.resolve_stage_artifact(
                service._run_id_from_ref(details["plan_ref"]), "script_plan"
            ).path.read_text(encoding="utf-8")
        )
        self.assertEqual(plan["brief"]["项目名称"], "匿名乙")
        self.assertEqual(plan["brief"]["品牌或客户"], "品牌乙")
        for key in ("script_rows", "detail_rows", "decisions", "template_slots", "shot_plans"):
            self.assertEqual(len(plan[key]), preview["shot_count"])
        provenance = {
            item["fact_id"]: item
            for item in plan["confirmed_brief_provenance"]["facts"]
        }
        for conflict in resolved.payload_data["conflicts"]:
            selected = provenance[conflict["resolved_fact_id"]]
            self.assertEqual(
                selected["conflict_resolution"]["resolution_evidence"],
                conflict["resolution_evidence"],
            )
            rejected = set(conflict["fact_ids"]) - {conflict["resolved_fact_id"]}
            self.assertEqual(
                {provenance[fact_id]["consumption"] for fact_id in rejected},
                {"conflict_candidate_not_selected"},
            )
            raw = self.briefs.artifacts.read(
                ArtifactPayload.from_mapping(conflict["resolution_evidence"])
            )
            evidence = json.loads(raw.decode("utf-8"))
            self.assertNotEqual(
                evidence["selected_fact_id"], conflict["resolved_fact_id"]
            )
            self.assertEqual(
                evidence["resolved_by_actor_id"], conflict["resolved_by_actor_id"]
            )
            self.assertEqual(evidence["resolved_at"], conflict["resolved_at"])
        output = self.root / "two-resolved-conflicts.xlsx"
        write_template_output_workbook(output, plan["script_rows"], plan["detail_rows"], [])
        workbook = load_workbook(output, data_only=True)
        self.addCleanup(workbook.close)
        self.assertEqual(
            workbook[ADJACENT_REVIEW_SHEET_NAME].max_row - 1,
            preview["shot_count"] - 1,
        )
        self.assertEqual(self._table_count("project_versions"), 0)

    def test_planning_reuses_current_project_name_without_rewriting_confirmed_brief(self):
        confirmed = self._confirmed_brief(expected_project_version=2, omit_fields=("项目名称",))
        calls = []
        policy = FormalTextProviderPolicy(
            writer=self._provider("writer", calls), editor=self._provider("editor", calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        )
        service = self._service(policy)
        original = self.briefs.resolve_confirmed(self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], confirmed=confirmed)
        preview = service.prepare_plan(self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=4, generation_mode="text_only_draft")
        self.assertEqual(preview["status"], "waiting_user")
        event = service._plan_event(self.token, preview["plan_id"])
        run_id = service._run_id_from_ref(event["plan_ref"])
        plan = json.loads((self.root / "runs" / self.project["project_id"] / "runs" / run_id / "script_plan.json").read_text())
        self.assertEqual(plan["brief"]["项目名称"], self.project["name"])
        self.assertEqual(plan["brief"]["_formal_project_context"]["project_id"], self.project["project_id"])
        self.assertEqual(plan["constraints"]["formal_project_context"], plan["brief"]["_formal_project_context"])
        self.assertNotIn("项目名称", plan["brief"]["_formal_fact_evidence"])
        self.assertEqual(original, self.briefs.resolve_confirmed(self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], confirmed=confirmed))
        self.expected_shot_count = preview["shot_count"]
        result = service.confirm_plan_and_generate(self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], plan_id=preview["plan_id"], expected_project_version=5)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["script_rows"]), preview["shot_count"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(self._table_count("project_versions"), 1)


    def test_planning_readiness_reports_confirmed_missing_fields_without_writes_and_recovers(self):
        self._confirmed_brief(expected_project_version=2, omit_fields=("项目名称", "视频时长"))
        service = self._service()
        tables = ("tasks", "task_events", "project_runs", "project_versions", "brief_versions")
        before = {name: self._table_count(name) for name in tables}
        readiness = service.get_planning_readiness(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=4,
        )
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["code"], "confirmed_brief_needs_clarification")
        self.assertIn("视频时长", readiness["message"])
        self.assertNotIn("项目名称", readiness["message"])
        self.assertEqual(before, {name: self._table_count(name) for name in tables})
        for mode in ("text_only_draft", "image_backed"):
            with self.assertRaises(FormalGenerationBlocked) as blocked:
                service.prepare_plan(self.token, workspace_id=self.workspace_id,
                    project_id=self.project["project_id"], expected_project_version=4, generation_mode=mode)
            self.assertEqual(blocked.exception.code, readiness["code"])
            self.assertEqual(blocked.exception.message, readiness["message"])
        self._confirmed_brief(expected_project_version=4)
        refreshed = service.get_planning_readiness(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=6,
        )
        self.assertTrue(refreshed["ready"])
        preview = service.prepare_plan(self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=6, generation_mode="text_only_draft")
        self.assertEqual(preview["status"], "waiting_user")
        self.assertGreater(preview["shot_count"], 2)

    def test_confirmed_brief_preview_then_explicit_confirmation_generates_and_restarts(self):
        calls = []
        policy = FormalTextProviderPolicy(
            writer=self._provider("writer", calls), editor=self._provider("editor", calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        )
        service = self._service(policy)
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        public = json.dumps(preview, ensure_ascii=False)
        self.assertEqual(preview["status"], "waiting_user")
        self.assertGreater(preview["shot_count"], 2)
        self.assertEqual(len(preview["shots"]), preview["shot_count"])
        self.assertTrue(any(shot["visual_status"] == "pending_draft" for shot in preview["shots"]))
        self.assertTrue(all(not shot["visual"] for shot in preview["shots"]
                            if shot["visual_status"] == "pending_draft"))
        self.assertEqual(len(preview["events"]), 3)
        self.assertNotIn("artifact://", public)
        self.assertRegex(preview["plan_sha256"], r"^[0-9a-f]{64}$")
        public_without_plan_sha = json.dumps(
            {key: value for key, value in preview.items() if key != "plan_sha256"},
            ensure_ascii=False,
        )
        self.assertNotIn("sha256", public_without_plan_sha.casefold())
        self.assertNotIn("workspace", public.casefold())
        self.assertEqual(calls, [])
        self.expected_shot_count = preview["shot_count"]
        plan_task = service.services.tasks.get_task(self.token, preview["plan_id"])
        plan_events = service.services.tasks.list_task_events(self.token, preview["plan_id"])
        plan_details = plan_events[-1]["details"]
        self.assertEqual(plan_task["status"], "waiting_user")
        self.assertTrue(plan_details["plan_ref"].startswith("artifact://run/"))
        self.assertTrue(plan_details["creative_standard_version_id"])
        self.assertTrue(plan_details["creative_standard_confirmation_id"])
        self.assertEqual(plan_details["creative_standard_brief_snapshot_id"], self.confirmed.brief_snapshot_id)
        self.assertTrue(plan_details["creative_standard_card_payload_ref"].startswith("artifact://"))
        parent_run = service._run_id_from_ref(plan_details["plan_ref"])
        parent_manifest = service.services.artifacts.read_manifest(parent_run)
        self.assertEqual(parent_manifest["status"], "waiting_user")
        self.assertEqual(parent_manifest["stages"]["script_draft"]["attempt"], 0)
        plan_raw = json.loads(
            service.services.artifacts.resolve_stage_artifact(parent_run, "script_plan").path.read_text(encoding="utf-8")
        )
        consumed = plan_raw["planning_audit"]["creative_standard_consumption"]
        self.assertTrue(consumed["tone_keywords"])
        self.assertTrue(consumed["expression_focus"])
        self.assertTrue(consumed["avoid_expressions"])
        self.assertIn("confirmed_creative_standard_provenance", plan_raw)

        restarted_store = WorkspaceStore(self.store.database_path)
        restarted_briefs = FormalBriefWorkspaceService(restarted_store, artifact_root=self.root / "runtime")
        restarted_service = self._service(policy, store=restarted_store, briefs=restarted_briefs)
        recovered = restarted_service.get_plan_preview(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            plan_id=preview["plan_id"], expected_project_version=3,
        )
        self.assertEqual(recovered, preview)
        self.assertEqual(
            restarted_service.list_waiting_plans(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                expected_project_version=3,
            ),
            {"items": [{"plan_id": preview["plan_id"], "status": "waiting_user"}]},
        )

        result = restarted_service.confirm_plan_and_generate(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            plan_id=preview["plan_id"], expected_project_version=3,
        )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["version_id"])
        self.assertEqual(len(result["script_rows"]), preview["shot_count"])
        self.assertEqual(len(calls), 2)
        for row in result["script_rows"]:
            self.assertTrue(str(row.get("画面描述") or "").strip())
            self.assertTrue(str(row.get("声音") or "").strip())
            self.assertTrue(row.get("时长(秒)"))
        for shot in preview["shots"]:
            self.assertTrue(shot["primary_event"])
            self.assertTrue(shot["visual"] or shot["visual_status"] == "pending_draft")
            self.assertTrue(shot["sound"])
            self.assertTrue(shot["duration_seconds"])
            self.assertTrue(shot["source"]["file"])
            self.assertTrue(shot["source"]["sheet"])
            self.assertTrue(shot["source"]["row"])
        task_events = TaskService(restarted_store).list_task_events(self.token, preview["plan_id"])
        self.assertEqual(task_events[-1]["details"]["provider_calls"], 2)
        self.assertEqual(task_events[-1]["details"]["writer_provider"], "anonymous-writer")
        current = ProjectService(restarted_store).get_project(self.token, self.project["project_id"])
        self.assertEqual(current["current_version_id"], result["version_id"])
        self.assertEqual(current["project_version"], 5)
        with self.assertRaises(FormalGenerationBlocked) as duplicate:
            restarted_service.confirm_plan_and_generate(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project["project_id"],
                plan_id=preview["plan_id"],
                expected_project_version=5,
            )
        self.assertEqual(duplicate.exception.code, "PLAN_CONFIRMATION_REQUIRED")
        self.assertEqual(self._table_count("project_versions"), 1)
        self.assertEqual(len(calls), 2)
        activity = self._service(
            policy,
            store=WorkspaceStore(self.store.database_path),
            briefs=FormalBriefWorkspaceService(
                WorkspaceStore(self.store.database_path),
                artifact_root=self.root / "runtime",
            ),
        ).list_project_activity(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
        )
        self.assertEqual((activity["count"], activity["items"][0]["state"]), (1, "completed"))
        self.assertEqual(activity["items"][0]["title"], "规划与脚本生成")
        self.assertEqual(activity["items"][0]["action_label"], "查看脚本")
        self.assertEqual(
            activity["items"][0]["action_href"],
            f"/projects/{self.project['project_id']}/script",
        )
        rendered_activity = json.dumps(activity, ensure_ascii=False).casefold()
        for forbidden in (
            "task_id", "agent_session", "payload_sha", "result_ref",
            "artifact://", "provider", "model", "path", "actor",
        ):
            self.assertNotIn(forbidden, rendered_activity)

        # Later approvals affect future work, never the completed run's frozen input.
        frozen_files = {str(path.relative_to(self.root / 'runs')): path.read_bytes()
            for path in (self.root / 'runs').rglob('*') if path.is_file()}
        self.assertTrue(any(name.endswith('input_freeze.json') for name in frozen_files))
        with self.store.connection() as connection:
            versions_before = [tuple(row) for row in connection.execute('SELECT * FROM project_versions')]
        self._approve_project_rule('后续生成新增规则，旧生成依据不得回写')
        self.assertEqual({str(path.relative_to(self.root / 'runs')): path.read_bytes()
            for path in (self.root / 'runs').rglob('*') if path.is_file()}, frozen_files)
        with self.store.connection() as connection:
            self.assertEqual([tuple(row) for row in connection.execute('SELECT * FROM project_versions')], versions_before)
            completed_run = connection.execute('SELECT run_id FROM project_versions WHERE version_id=?',
                (result['version_id'],)).fetchone()[0]
        self.assertEqual(restarted_service.services.artifacts.read_manifest(completed_run)['status'], 'completed')
        self.assertEqual(len(calls), 2)

    def test_generated_structure_only_hides_fact_label_from_provider_and_keeps_audit_strict(self):
        self._confirm_adapter_brief()
        knowledge_statement = "每镜优先使用可拍动作，不使用抽象判断"
        self._approve_project_rule(knowledge_statement)
        prompts = []

        def structure_sensitive_provider(prompt, _model, _max_tokens):
            prompts.append(prompt)
            source = json.loads(
                prompt.split("\nDATA:", 1)[1].split("\nDRAFT:", 1)[0]
            )
            shots = []
            for item in source["shots"]:
                contract = item.get("planning_contract") or {}
                primary = str(contract.get("primary_event") or "中性节奏推进")
                if contract.get("generated_structure_only"):
                    movements = [
                        "沿左侧缓推", "从右侧平移", "自低处抬升", "向远处拉开",
                        "跟随光线平移", "从高处缓降", "固定机位留白", "沿边缘侧移",
                        "从近处退后", "缓推至构图中心", "从边角转向中心", "由远处缓推",
                        "贴近前景横移", "沿纵深后退", "固定机位记录光影", "从中心缓缓拉远",
                        "低机位缓推", "平移至前景边缘",
                    ]
                    primary = f"镜头{movements[int(item['shot_no']) - 1]}，前景与远景随构图展开"
                leaked = (
                    contract.get("generated_structure_only") is True
                    and any(term in str(item.get("structure") or "") for term in ("宣讲", "发言", "演讲"))
                )
                shots.append({
                    "shot_no": str(item["shot_no"]),
                    "visual_description": (
                        f"{primary}，演讲者在台上说明内容" if leaked
                        else f"{primary}，以中性环境和构图推进节奏"
                    ),
                    "sound": "现场环境声",
                    "voiceover": "",
                    "subtitle": "",
                    "pending_confirmations": [],
                })
            return json.dumps({
                "schema_version": "mplus-script-draft-response/v1",
                "overall_summary": "依据已确认事件形成匿名现场叙事。",
                "shots": shots,
            }, ensure_ascii=False)

        service = self._service(FormalTextProviderPolicy(
            writer=structure_sensitive_provider,
            editor=structure_sensitive_provider,
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        ))
        preview = service.prepare_plan(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            expected_project_version=4,
            generation_mode="text_only_draft",
        )
        result = service.confirm_plan_and_generate(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            plan_id=preview["plan_id"],
            expected_project_version=5,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["script_rows"]), preview["shot_count"])
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all(knowledge_statement in prompt for prompt in prompts))
        event = service._plan_event(self.token, preview["plan_id"])
        plan = json.loads(service.services.artifacts.resolve_stage_artifact(
            service._run_id_from_ref(event["plan_ref"]), "script_plan",
        ).path.read_text(encoding="utf-8"))
        generated_shots = {
            str(item["shot_no"])
            for item in plan["shot_plans"]
            if item.get("generated_structure_only")
        }
        self.assertTrue(generated_shots)
        for row in result["script_rows"]:
            if str(row["镜号"]) in generated_shots:
                self.assertNotIn("演讲", str(row.get("画面描述") or ""))
                self.assertNotIn("完整呈现", str(row.get("画面描述") or ""))
                self.assertIn("前景与远景随构图展开", row["画面描述"])

    def test_effective_knowledge_is_frozen_consumed_and_drift_blocks_before_provider(self):
        first = self._approve_project_rule("全片保持克制表达，避免口号式喊声")
        calls = []
        prompts = []

        def provider(prompt, model, max_tokens):
            prompts.append(prompt)
            return self._provider("knowledge", calls)(prompt, model, max_tokens)

        service = self._service(FormalTextProviderPolicy(
            writer=provider, editor=provider,
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        ))
        preview = service.prepare_plan(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        event = service._plan_event(self.token, preview["plan_id"])
        parent_run = service._run_id_from_ref(event["plan_ref"])
        manifest = service.services.artifacts.read_manifest(parent_run)
        plan = json.loads(service.services.artifacts.resolve_stage_artifact(
            parent_run, "script_plan",
        ).path.read_text(encoding="utf-8"))
        frozen = json.loads((
            self.root / "runs" / self.project["project_id"] / "runs" / parent_run / "input_freeze.json"
        ).read_text(encoding="utf-8"))
        snapshot = plan["constraints"]["effective_knowledge"]
        self.assertEqual(snapshot["rules"][0]["rule_id"], first["rule_id"])
        self.assertEqual(
            plan["planning_audit"]["knowledge_rule_consumption"]["snapshot_sha256"],
            snapshot["snapshot_sha256"],
        )
        self.assertEqual(
            manifest["run_provenance"]["effective_knowledge"]["snapshot_sha256"],
            snapshot["snapshot_sha256"],
        )
        self.assertEqual(
            frozen["config"]["effective_knowledge"]["snapshot_sha256"],
            snapshot["snapshot_sha256"],
        )

        self._approve_project_rule("每镜优先使用可拍动作，不使用抽象判断")
        with self.assertRaises(FormalGenerationBlocked) as caught:
            service.confirm_plan_and_generate(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project["project_id"],
                plan_id=preview["plan_id"],
                expected_project_version=3,
            )
        self.assertEqual(caught.exception.code, "KNOWLEDGE_RULE_DRIFT")
        self.assertEqual(calls, [])
        self.assertEqual(prompts, [])
        self.assertEqual(self._table_count("project_versions"), 0)

    def test_formal_package_identity_is_frozen_in_all_run_evidence_and_drift_fails_closed(self):
        workbook_path = self.source_root / "M+发布会类通用脚本库.xlsx"
        embedded_stream = BytesIO()
        with PILImage.new("RGB", (32, 32), color="blue") as embedded:
            embedded.save(embedded_stream, format="PNG")
        embedded_stream.seek(0)
        workbook = load_workbook(workbook_path)
        workbook["模版"].add_image(ExcelImage(embedded_stream), "C7")
        workbook.save(workbook_path)
        workbook.close()
        embedded_stream.close()
        refresh_ai_spec(workbook_path)
        editable = create_asset_package(
            self.root / "editable-package",
            package_id="launch-core",
            source_root=self.source_root,
        )
        package = bundle_asset_package_v3(
            editable,
            self.root / "formal-package-v4",
            package_version=4,
        )
        record = {
            **build_package_record(package),
            "alias": "formal",
            "resolved_ref": "launch-core@4",
        }
        package_root = Path(record["local_path"])
        self.assertFalse(list(package_root.rglob("_mvp_v2_extracted_images")))
        verify_content = {"enabled": False}

        def resolve_package(_script_type):
            if verify_content["enabled"]:
                current = build_package_record(package)
                if current["package_sha256"] != record["package_sha256"]:
                    raise PackageRegistryError("资产包内容与注册 SHA-256 不一致")
            return record

        service = self._service(asset_package_resolver=resolve_package)
        with patch("mvp_v2.formal_generation_service.build_package_record") as rehash:
            preview = service.prepare_plan(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project["project_id"],
                expected_project_version=2,
                generation_mode="text_only_draft",
            )
        rehash.assert_not_called()
        verify_content["enabled"] = True
        self.assertEqual(
            build_package_record(package)["package_sha256"],
            record["package_sha256"],
        )
        self.assertFalse(list(package_root.rglob("_mvp_v2_extracted_images")))
        restarted_store = WorkspaceStore(self.store.database_path)
        restarted_service = self._service(
            store=restarted_store,
            briefs=FormalBriefWorkspaceService(
                restarted_store, artifact_root=self.root / "runtime",
            ),
            asset_package_resolver=resolve_package,
        )
        recovered = restarted_service.get_plan_preview(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            plan_id=preview["plan_id"],
            expected_project_version=3,
        )
        self.assertEqual(recovered, preview)
        event = service._plan_event(self.token, preview["plan_id"])
        run_id = service._run_id_from_ref(event["plan_ref"])
        run_dir = self.root / "runs" / self.project["project_id"] / "runs" / run_id
        manifest = service.services.artifacts.read_manifest(run_id)
        frozen = json.loads((run_dir / "input_freeze.json").read_text(encoding="utf-8"))
        catalog = json.loads((run_dir / "source_catalog.json").read_text(encoding="utf-8"))
        plan = json.loads((run_dir / "script_plan.json").read_text(encoding="utf-8"))
        expected = {
            key: record[key]
            for key in ("package_id", "package_version", "package_sha256", "alias", "resolved_ref")
        }
        self.assertEqual(manifest["run_provenance"]["formal_asset_package"], expected)
        self.assertEqual(frozen["formal_asset_package"], expected)
        self.assertEqual(catalog["formal_asset_package"], expected)
        self.assertEqual(plan["constraints"]["formal_asset_package"], expected)

        source_workbook = Path(asset_package_source_record(package)["source_workbook_path"])
        source_workbook.write_bytes(source_workbook.read_bytes() + b"drift")
        with self.assertRaises(FormalGenerationBlocked):
            service.confirm_plan_and_generate(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project["project_id"],
                plan_id=preview["plan_id"],
                expected_project_version=3,
            )
        self.assertEqual(self._table_count("project_versions"), 0)

    def test_hard_template_conflict_brief_wins_replans_without_model_or_version(self):
        self._confirmed_brief(
            expected_project_version=2,
            required_content="开头核心发言",
            event_titles=["开头核心发言", "品牌收束"],
        )
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=4,
            generation_mode="text_only_draft",
        )
        self.assertEqual(preview["audit"]["status"], "needs_clarification")
        self.assertGreater(preview["decision_counts"]["unresolved_conflicts"], 0)
        resolutions = [
            {"conflict_id": item["conflict_id"], "choice": "brief_wins"}
            for item in preview["conflicts"]
        ]
        prepared = service.prepare_conflict_resolutions(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], plan_id=preview["plan_id"],
            plan_sha256=preview["plan_sha256"], expected_project_version=5,
            idempotency_key="brief-wins-once", resolutions=resolutions,
        )
        self.assertEqual((prepared["risk"], prepared["model_calls"]), ("R2", 0))
        result = service.execute_conflict_resolutions(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], plan_id=preview["plan_id"],
            plan_sha256=preview["plan_sha256"], expected_project_version=5,
            idempotency_key="brief-wins-once", resolutions=resolutions,
            command_token=prepared["command_token"],
        )
        self.assertEqual((result["model_calls"], result["script_versions_created"]), (0, 0))
        self.assertEqual(result["new_plan"]["decision_counts"]["unresolved_conflicts"], 0)
        self.assertGreater(result["new_plan"]["decision_counts"]["hard_template_downgrades"], 0)
        summary = result["new_plan"]["template_summary"]
        self.assertEqual(summary["structure_note"], "已按确认需求调整段落")
        self.assertIn({"kind": "structure", "shot_no": "1", "field": "structure",
            "before": "开场造势", "after": "开头核心发言",
            "summary": "第 1 镜段落：开场造势 → 开头核心发言"}, summary["adjustments"])
        self.assertIn("第 1 镜：开头核心发言", [item["summary"] for item in summary["adjustments"]])
        self.assertEqual(self._service().get_plan_preview(self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], plan_id=result["new_plan"]["plan_id"])["template_summary"], summary)
        self.assertFalse(result["derived_brief_version_id"])
        self.assertEqual(self._table_count("project_versions"), 0)
        with self.assertRaises(WorkspaceServiceError):
            service.execute_conflict_resolutions(
                self.token, workspace_id=self.workspace_id,
                project_id=self.project["project_id"], plan_id=preview["plan_id"],
                plan_sha256=preview["plan_sha256"], expected_project_version=5,
                idempotency_key="brief-wins-once", resolutions=resolutions,
                command_token=prepared["command_token"],
            )

    def test_conflict_child_plan_preserves_script_replan_target(self):
        self._confirmed_brief(
            expected_project_version=2,
            required_content="开头核心发言",
            event_titles=["开头核心发言", "品牌收束"],
        )
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=4,
            generation_mode="text_only_draft",
        )
        resolutions = [
            {"conflict_id": item["conflict_id"], "choice": "brief_wins"}
            for item in preview["conflicts"]
        ]
        prepared = service.prepare_conflict_resolutions(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], plan_id=preview["plan_id"],
            plan_sha256=preview["plan_sha256"], expected_project_version=5,
            idempotency_key="replan-conflict-preserve", resolutions=resolutions,
        )
        source_event = service._plan_event(self.token, preview["plan_id"])
        source_event["script_replan_intent"] = {
            "revision_task_id": "script-replan-task-preserve",
            "shot_count_before": 18,
            "shot_count_after": 20,
        }
        with patch.object(service, "_plan_event", return_value=source_event), patch.object(
            service, "prepare_plan", return_value={"plan_id": "child-plan-preserve"}
        ) as prepare_plan:
            service.execute_conflict_resolutions(
                self.token, workspace_id=self.workspace_id,
                project_id=self.project["project_id"], plan_id=preview["plan_id"],
                plan_sha256=preview["plan_sha256"], expected_project_version=5,
                idempotency_key="replan-conflict-preserve", resolutions=resolutions,
                command_token=prepared["command_token"],
            )

        self.assertEqual(
            prepare_plan.call_args.kwargs["revision_task_id"],
            "script-replan-task-preserve",
        )

    def test_hard_template_conflict_template_wins_derives_brief_without_overwrite(self):
        original = self._confirmed_brief(
            expected_project_version=2,
            required_content="开头核心发言",
            event_titles=["开头核心发言", "品牌收束"],
        )
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=4,
            generation_mode="text_only_draft",
        )
        resolutions = [
            {"conflict_id": item["conflict_id"], "choice": "template_wins"}
            for item in preview["conflicts"]
        ]
        prepared = service.prepare_conflict_resolutions(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], plan_id=preview["plan_id"],
            plan_sha256=preview["plan_sha256"], expected_project_version=5,
            idempotency_key="template-wins-once", resolutions=resolutions,
        )
        result = service.execute_conflict_resolutions(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], plan_id=preview["plan_id"],
            plan_sha256=preview["plan_sha256"], expected_project_version=5,
            idempotency_key="template-wins-once", resolutions=resolutions,
            command_token=prepared["command_token"],
        )
        self.assertTrue(result["derived_brief_version_id"])
        self.assertNotEqual(result["derived_brief_version_id"], original.brief_version_id)
        self.assertEqual(result["new_plan"]["decision_counts"]["unresolved_conflicts"], 0)
        self.assertEqual(result["new_plan"]["decision_counts"]["hard_template_downgrades"], 0)
        self.assertGreater(result["new_plan"]["decision_counts"]["requirements_uncovered"], 0)
        with self.store.connection() as connection:
            original_row = connection.execute(
                """SELECT v.payload_sha256, c.confirmation_id
                   FROM brief_versions v
                   JOIN brief_confirmations c ON c.brief_version_id=v.brief_version_id
                   WHERE v.brief_version_id=?""",
                (original.brief_version_id,),
            ).fetchone()
        self.assertEqual(original_row["payload_sha256"], original.payload.sha256)
        self.assertTrue(original_row["confirmation_id"])
        self.assertEqual(self._table_count("project_versions"), 0)

    def test_clarification_plan_is_saved_without_provider_or_version(self):
        self._confirmed_brief(
            expected_project_version=2,
            event_titles=["开场", "艺人身份待确认", "收束"],
        )
        calls = []
        policy = FormalTextProviderPolicy(
            writer=self._provider("writer", calls), editor=self._provider("editor", calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        )
        service = self._service(policy)
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=4,
            generation_mode="text_only_draft",
        )

        self.assertEqual(preview["status"], "waiting_user")
        self.assertTrue(preview["needs_clarification"])
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(FormalGenerationBlocked, "待确认事实"):
            service.confirm_plan_and_generate(
                self.token, workspace_id=self.workspace_id,
                project_id=self.project["project_id"],
                plan_id=preview["plan_id"], expected_project_version=5,
            )
        self.assertEqual(calls, [])
        self.assertEqual(self._table_count("project_versions"), 0)

    def test_planning_contract_failure_is_persisted_as_actionable_task(self):
        service = self._service()
        with patch.object(
            service,
            "_planning_brief",
            side_effect=ValueError("不支持的脚本类型: 互动视频"),
        ), self.assertRaises(FormalGenerationBlocked) as caught:
            service.prepare_plan(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project["project_id"],
                expected_project_version=2,
                generation_mode="text_only_draft",
            )

        self.assertEqual(caught.exception.code, "SCRIPT_TYPE_UNSUPPORTED")
        tasks = TaskService(self.store).list_project_tasks(
            self.token, self.project["project_id"]
        )
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "blocked")
        self.assertEqual(tasks[0]["error_code"], "SCRIPT_TYPE_UNSUPPORTED")
        activity = service.list_project_activity(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
        )
        self.assertEqual(activity["count"], 1)
        self.assertEqual(activity["items"][0]["state_label"], "已阻断")
        self.assertIn("脚本类型尚不支持", activity["items"][0]["issue"])
        self.assertEqual(activity["items"][0]["action_label"], "查看并处理")
        self.assertEqual(
            activity["items"][0]["action_href"],
            f"/projects/{self.project['project_id']}/brief",
        )

    def test_preview_blocks_cross_project_old_brief_lock_and_tampered_artifact(self):
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        other = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名其他项目",
            category_id="launch",
            expected_workspace_version=self.projects.get_workspace(self.token, self.workspace_id)["workspace_version"],
        )
        with self.assertRaisesRegex(FormalGenerationBlocked, "不属于"):
            service.get_plan_preview(
                self.token, workspace_id=self.workspace_id, project_id=other["project_id"],
                plan_id=preview["plan_id"],
            )
        with self.assertRaisesRegex(FormalGenerationBlocked, "lock"):
            service.get_plan_preview(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                plan_id=preview["plan_id"], expected_project_version=2,
            )
        events = service.services.tasks.list_task_events(self.token, preview["plan_id"])
        event = next(value["details"] for value in events if value["details"].get("plan_ref"))
        run_id = service._run_id_from_ref(event["plan_ref"])
        plan_file = service.services.artifacts.resolve_stage_artifact(run_id, "script_plan").path
        plan_file.write_bytes(plan_file.read_bytes() + b"tampered")
        with self.assertRaisesRegex(FormalGenerationBlocked, "不可用或已漂移"):
            service.get_plan_preview(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                plan_id=preview["plan_id"], expected_project_version=3,
            )

    def test_preview_blocks_when_current_brief_snapshot_changes(self):
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        with self.store.transaction() as connection:
            connection.execute(
                """UPDATE projects
                   SET current_brief_snapshot_id = NULL,
                       current_creative_standard_version_id = NULL
                   WHERE project_id = ?""",
                (self.project["project_id"],),
            )
        with self.assertRaisesRegex(FormalGenerationBlocked, "没有已确认 Brief"):
            service.get_plan_preview(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                plan_id=preview["plan_id"], expected_project_version=3,
            )

    def test_preview_blocks_forged_event(self):
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        with self.store.transaction() as connection:
            sequence = connection.execute(
                "SELECT MAX(sequence_no) + 1 FROM task_events WHERE task_id = ?",
                (preview["plan_id"],),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO task_events(
                    task_event_id, task_id, sequence_no, status, details_json, created_at
                ) VALUES (?, ?, ?, 'waiting_user', ?, ?)""",
                (
                    new_id("task_event"), preview["plan_id"], sequence,
                    json.dumps({"action": "prepare_plan", "plan_ref": "artifact://run/forged/script_plan"}),
                    utc_now(),
                ),
            )
        with self.assertRaisesRegex(FormalGenerationBlocked, "绑定不一致"):
            service.get_plan_preview(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                plan_id=preview["plan_id"], expected_project_version=3,
            )

    def test_preview_blocks_when_current_confirmed_brief_advances(self):
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        replacement = self._confirmed_brief(expected_project_version=3)
        self.assertNotEqual(replacement.brief_snapshot_id, self.confirmed.brief_snapshot_id)
        with self.assertRaisesRegex(FormalGenerationBlocked, "绑定不一致"):
            service.get_plan_preview(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                plan_id=preview["plan_id"], expected_project_version=5,
            )

    def test_preview_detects_lock_toctou_without_expected_lock(self):
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        original = service.services.artifacts.resolve_manifest

        def mutate_then_resolve(*args, **kwargs):
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE projects SET lock_version = lock_version + 1 WHERE project_id = ?",
                    (self.project["project_id"],),
                )
            return original(*args, **kwargs)

        with patch.object(service.services.artifacts, "resolve_manifest", side_effect=mutate_then_resolve):
            with self.assertRaisesRegex(FormalGenerationBlocked, "读取规划期间"):
                service.get_plan_preview(
                    self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                    plan_id=preview["plan_id"],
                )

    def test_waiting_plan_list_filters_another_actor_and_preview_requires_task_owner(self):
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        identity = LocalIdentityAdapter(self.store)
        colleague = identity.create_actor(
            self.token,
            workspace_id=self.workspace_id,
            display_name="匿名同事",
            auth_source="local-test",
            role="viewer",
            expected_workspace_version=self.projects.get_workspace(
                self.token, self.workspace_id
            )["workspace_version"],
        )
        colleague_token = identity.issue_auth_session(
            actor_id=colleague["actor_id"], token="formal-generation-colleague"
        )["auth_token"]
        self.assertEqual(
            service.list_waiting_plans(
                colleague_token,
                workspace_id=self.workspace_id,
                project_id=self.project["project_id"],
                expected_project_version=3,
            ),
            {"items": []},
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "不能读取其他操作者"):
            service.get_plan_preview(
                colleague_token,
                workspace_id=self.workspace_id,
                project_id=self.project["project_id"],
                plan_id=preview["plan_id"],
                expected_project_version=3,
            )

    def test_preconditions_and_missing_provider_block_without_draft_or_live_calls(self):
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        with self.assertRaisesRegex(FormalGenerationBlocked, "未配置"):
            service.confirm_plan_and_generate(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                plan_id=preview["plan_id"], expected_project_version=3,
            )
        events = service.services.tasks.list_task_events(self.token, preview["plan_id"])
        self.assertEqual(events[-1]["details"]["provider_calls"], 0)
        plan_event = next(event for event in events if event["details"].get("action") == "prepare_plan" and event["details"].get("plan_ref"))
        parent_run = service._run_id_from_ref(plan_event["details"]["plan_ref"])
        manifest = service.services.artifacts.read_manifest(parent_run)
        self.assertEqual(manifest["stages"]["script_draft"]["attempt"], 0)

    def test_prepare_plan_persists_optional_outer_operation_key_on_authority_event(self):
        service = self._service()
        operation_key = "task_outer_prepare_binding"
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2, generation_mode="text_only_draft",
            operation_key=operation_key,
        )

        events = service.services.tasks.list_task_events(self.token, preview["plan_id"])
        plan_event = next(
            event for event in events
            if event["details"].get("action") == "prepare_plan"
            and event["details"].get("plan_ref")
        )
        self.assertEqual(plan_event["details"]["operation_key"], operation_key)
        self.assertEqual(plan_event["details"]["outer_task_id"], operation_key)
        self.assertEqual(plan_event["details"]["project_id"], self.project["project_id"])
        self.assertEqual(plan_event["details"]["expected_project_version"], 2)

    def test_missing_image_authority_creates_blocked_task_before_p00(self):
        service = FormalGenerationService(
            FormalGenerationServices(
                briefs=self.briefs, projects=self.projects, tasks=TaskService(self.store),
                artifacts=RunArtifactResolver(self.root / "runs"), images=None,
            ),
            runs_root=self.root / "runs", source_root=self.source_root,
        )
        with self.assertRaisesRegex(FormalGenerationBlocked, "图片权威服务"):
            service.prepare_plan(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                expected_project_version=2,
                generation_mode="text_only_draft",
            )
        with self.store.connection() as connection:
            task = connection.execute(
                "SELECT task_id, status FROM tasks WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                (self.project["project_id"],),
            ).fetchone()
        self.assertEqual(task["status"], "blocked")
        details = service.services.tasks.list_task_events(self.token, task["task_id"])[-1]["details"]
        self.assertEqual(
            {key: details[key] for key in ("p00_attempt", "script_draft_attempt", "provider_calls", "version_created")},
            {"p00_attempt": 0, "script_draft_attempt": 0, "provider_calls": 0, "version_created": 0},
        )

    def test_each_nonpass_image_state_blocks_task_before_p00_or_version(self):
        for status in ("pending", "low_confidence", "blocked", "failed"):
            with self.subTest(status=status):
                service = self._service()
                counts = {"pending": 0, "low_confidence": 0, "blocked": 0, "failed": 0}
                counts[status] = 1
                readiness = ProjectImagePlanningReadiness(
                    self.workspace_id, self.project["project_id"],
                    self.confirmed.brief_version_id, self.confirmed.brief_snapshot_id,
                    1, 0, counts["pending"], counts["low_confidence"], counts["blocked"], counts["failed"], (),
                )
                with patch.object(service, "_synchronize_and_resolve_images", return_value=readiness):
                    with self.assertRaisesRegex(FormalGenerationBlocked, "图片"):
                        service.prepare_plan(
                            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                            expected_project_version=2,
                            generation_mode="image_backed",
                        )
                with self.store.connection() as connection:
                    task = connection.execute(
                        "SELECT task_id, status FROM tasks WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                        (self.project["project_id"],),
                    ).fetchone()
                    version_count = connection.execute(
                        "SELECT COUNT(*) FROM project_versions WHERE project_id=?", (self.project["project_id"],),
                    ).fetchone()[0]
                self.assertEqual(task["status"], "blocked")
                details = service.services.tasks.list_task_events(self.token, task["task_id"])[-1]["details"]
                self.assertEqual(
                    {key: details[key] for key in ("p00_attempt", "script_draft_attempt", "provider_calls", "version_created")},
                    {"p00_attempt": 0, "script_draft_attempt": 0, "provider_calls": 0, "version_created": 0},
                )
                self.assertEqual(version_count, 0)

    def test_preview_and_confirm_reject_image_evidence_drift_before_provider(self):
        calls = []
        policy = FormalTextProviderPolicy(
            writer=self._provider("writer", calls), editor=self._provider("editor", calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        )
        service = self._service(policy)
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        drifted = ProjectImagePlanningReadiness(
            self.workspace_id, self.project["project_id"], self.confirmed.brief_version_id,
            self.confirmed.brief_snapshot_id, 1, 1, 0, 0, 0, 0, (),
        )
        with patch.object(service, "_resolve_images", return_value=drifted):
            with self.assertRaisesRegex(FormalGenerationBlocked, "图片分析绑定"):
                service.get_plan_preview(
                    self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                    plan_id=preview["plan_id"], expected_project_version=3,
                )
            with self.assertRaisesRegex(FormalGenerationBlocked, "图片分析绑定"):
                service.confirm_plan_and_generate(
                    self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                    plan_id=preview["plan_id"], expected_project_version=3,
                )
        self.assertEqual(calls, [])

    def test_five_format_same_sha_image_context_reaches_plan_and_writer_data(self):
        adapter = FormalBriefInputAdapter(self.briefs, data_root=self.root / "runtime")
        imported = adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2, _replace_document_ids={"anonymous_doc_0"}, uploads=[
                *_representative_uploads(),
                UploadedDocument("planning-fields.txt", BytesIO(
                    "项目名称: 匿名项目\n脚本类型: 发布会\n目标时长: 60秒\n"
                    "品牌或客户: 匿名品牌\n风格调性: 克制真实\n必须出现的内容: 产品演示\n"
                    "关键事件与流程: 嘉宾发布、说明发布重点、产品演示、品牌收束\n禁止或不能出现的内容: 虚构".encode("utf-8")
                )),
            ],
        )
        self.assertEqual(imported["brief"]["conflicts"], [], "Same-valued facts and an extended flow do not require a false conflict decision")
        confirmed = adapter.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            brief_version_id=imported["brief_key"], confirmed_at="2026-08-14T00:00:00Z",
            expected_project_version=3,
        )
        paired = self.briefs.resolve_current_paired(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
        )
        pool = ProjectImagePoolService(self.store, data_root=self.root / "runtime")
        pool.synchronize_confirmed_brief(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=paired.brief.brief_snapshot_id,
            expected_project_lock_version=4,
        )
        pending = pool.resolve_for_planning(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=paired.brief.brief_snapshot_id,
            expected_project_lock_version=4,
        )
        self.assertEqual((pending.total, pending.pending), (1, 1))
        vision_calls = []
        service = self._service(vision=FormalVisionProviderPolicy(
            invoke=lambda path: vision_calls.append(path) or ProjectImageVisionResult(
                "pass", "产品发布现场的真实画面", ("发布",), ("产品",),
                "发布会现场", "用于脚本镜头", "清晰", "匿名品牌", ("画面可见主体",),
            ),
            provider="minimax-cli", model="provider-managed",
            prompt_version="anonymous-vision-v1",
        ))
        automatic = service.analyze_pending_images(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=4,
        )
        self.assertEqual((automatic["pass"], automatic["pending"]), (1, 0))
        self.assertEqual(automatic["automatic_understanding"]["processed"], 1)
        self.assertEqual(len(vision_calls), 1)
        again = service.analyze_pending_images(
            self.token, workspace_id=self.workspace_id,
            project_id=self.project["project_id"], expected_project_version=4,
        )
        self.assertEqual(again["automatic_understanding"]["processed"], 0)
        self.assertEqual(len(vision_calls), 1, "已有分析的同 SHA 图片不得重复调用")
        ready = pool.resolve_for_planning(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=paired.brief.brief_snapshot_id,
            expected_project_lock_version=4,
        )
        self.assertEqual((ready.total, ready.passed, ready.pending), (1, 1, 0))
        self.assertEqual(ready.images[0].business_summary.evidence_status, "provider_generated")
        stored = pool.artifacts.read_analysis(ready.images[0].current_analysis.evidence)
        self.assertEqual(stored["analysis_origin"], "provider")
        self.assertNotIn("reviewed_by_actor_id", stored)
        prompts, calls = [], []
        def provider(prompt, model, max_tokens):
            prompts.append(prompt)
            return self._provider("captured", calls)(prompt, model, max_tokens)
        service = self._service(FormalTextProviderPolicy(
            writer=provider, editor=provider,
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        ))
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=4,
            generation_mode="text_only_draft",
        )
        self.expected_shot_count = preview["shot_count"]
        details = service.services.tasks.list_task_events(self.token, preview["plan_id"])[-1]["details"]
        run_id = service._run_id_from_ref(details["plan_ref"])
        run_manifest = service.services.artifacts.read_manifest(run_id)
        self.assertEqual(
            run_manifest["cache_fingerprint"],
            sha256_json({
                "confirmed_brief": _confirmed_brief_evidence(paired.brief),
                "confirmed_creative_standard": paired.creative_standard.as_mapping(),
                "project_image_context": details["project_image_context"],
                "source_view": "ai-spec",
                "template_rules_sha256": "",
                "template_conflict_resolutions": {},
                "conflict_resolution_binding": {},
                "formal_asset_package": None,
                "effective_knowledge": details["effective_knowledge"],
            }),
        )
        summary = details["project_image_context"]["images"][0]["business_summary"]
        self.assertEqual(set(summary), {"caption", "tags", "subjects", "scene", "purpose", "quality", "brand", "candidate_only"})
        service.confirm_plan_and_generate(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            plan_id=preview["plan_id"], expected_project_version=5,
        )
        self.assertEqual(len(calls), 2)
        prompt_text = "\n".join(prompts)
        for field in ("caption", "tags", "subjects", "scene", "purpose", "quality", "brand"):
            self.assertIn(field, prompt_text)
        evidence = details["project_image_context"]
        image_evidence = evidence["images"][0]
        for secret in (
            "artifact://", "sha256", evidence["workspace_id"], evidence["project_id"],
            image_evidence["image_id"], image_evidence["analysis_ref"], image_evidence["analysis_sha256"],
            image_evidence["image_sha256"],
        ):
            self.assertNotIn(secret, prompt_text)

    def test_confirmed_plan_sha_mismatch_blocks_before_provider(self):
        calls = []
        policy = FormalTextProviderPolicy(
            writer=self._provider("writer", calls), editor=self._provider("editor", calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        )
        service = self._service(policy)
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        events = service.services.tasks.list_task_events(self.token, preview["plan_id"])
        plan = next(event["details"] for event in events if event["details"].get("plan_ref"))
        with self.assertRaisesRegex(FormalGenerationBlocked, "同一份规划"):
            service.generate_from_confirmed_plan(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                task_id=preview["plan_id"], plan_ref=plan["plan_ref"],
                plan_sha256="0" * 64,
                expected_project_version=3,
            )
        self.assertEqual(calls, [])
        task = service.services.tasks.get_task(self.token, preview["plan_id"])
        self.assertEqual(task["status"], "waiting_user")

    def test_old_lock_and_payload_tamper_block_before_provider(self):
        service = self._service()
        with self.assertRaisesRegex(FormalGenerationBlocked, "lock"):
            service.prepare_plan(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                expected_project_version=1,
                generation_mode="text_only_draft",
            )
        target = self.briefs.artifacts.root / self.confirmed.payload.sha256[:2] / f"{self.confirmed.payload.sha256}.json"
        target.write_bytes(target.read_bytes() + b"tampered")
        with self.assertRaises(FormalGenerationBlocked):
            service.prepare_plan(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                expected_project_version=2,
                generation_mode="text_only_draft",
            )

    def test_missing_or_tampered_creative_standard_blocks_before_task_or_provider(self):
        service = self._service()
        before_tasks = self._table_count("tasks")
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE projects SET current_creative_standard_version_id=NULL WHERE project_id=?",
                (self.project["project_id"],),
            )
        with self.assertRaises(FormalGenerationBlocked):
            service.prepare_plan(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                expected_project_version=2,
                generation_mode="text_only_draft",
            )
        self.assertEqual(self._table_count("tasks"), before_tasks)

        # A fresh anonymous project preserves the valid pair, then its stored card bytes drift.
        other = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名标准卡篡改项目",
            category_id="launch", expected_workspace_version=self.projects.get_workspace(
                self.token, self.workspace_id
            )["workspace_version"],
        )
        original_project = self.project
        self.project = other
        confirmed = self._confirmed_brief(expected_project_version=0)
        self.project = original_project
        with self.briefs.store.connection() as connection:
            card_row = connection.execute(
                "SELECT payload_sha256 FROM creative_standard_versions WHERE project_id=?",
                (other["project_id"],),
            ).fetchone()
        target = self.briefs.creative_standard_artifacts.root / card_row[0][:2] / f"{card_row[0]}.json"
        target.write_bytes(target.read_bytes() + b"tampered")
        with self.assertRaises(FormalGenerationBlocked):
            service.prepare_plan(
                self.token, workspace_id=self.workspace_id, project_id=other["project_id"],
                expected_project_version=2,
                generation_mode="text_only_draft",
            )
        self.assertTrue(confirmed.confirmation_id)

    def test_mandatory_creative_focus_is_bound_by_v3_capacity_planning(self):
        project = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名重点缺口项目",
            category_id="launch", expected_workspace_version=self.projects.get_workspace(
                self.token, self.workspace_id
            )["workspace_version"],
        )
        original_project = self.project
        self.project = project
        self._confirmed_brief(
            expected_project_version=0,
            required_content="空间建立、核心发布、品牌落版、未绑定重点",
        )
        self.project = original_project
        service = self._service()
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        self.assertEqual(preview["status"], "waiting_user")
        self.assertNotIn("creative_standard_focus_unbound", preview["audit"]["warning_codes"])
        traced = [
            item for item in preview["traceability"]
            if item["brief_text"] == "未绑定重点"
        ]
        self.assertEqual(len(traced), 1)
        self.assertEqual(traced[0]["final_status"], "covered")
        self.assertTrue(traced[0]["plan_shot_nos"])
        with self.store.connection() as connection:
            task = connection.execute(
                "SELECT status, error_code FROM tasks WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                (project["project_id"],),
            ).fetchone()
        self.assertEqual(task["status"], "waiting_user")
        self.assertIsNone(task["error_code"])

    def test_avoid_expression_in_client_review_draft_blocks_version_write(self):
        calls = []

        def forbidden_provider(_prompt, _model, _tokens):
            if "\nINPUT:" in _prompt:
                return "{}"
            calls.append("provider")
            return json.dumps({
                "schema_version": "mplus-script-draft-response/v1",
                "overall_summary": "匿名虚构事实总结",
                "shots": [
                    {"shot_no": str(index), "visual_description": "正常画面", "sound": "虚构事实的现场音乐", "voiceover": "", "subtitle": "", "pending_confirmations": []}
                    for index in range(1, self.expected_shot_count + 1)
                ],
            }, ensure_ascii=False)

        policy = FormalTextProviderPolicy(
            writer=forbidden_provider, editor=forbidden_provider,
            writer_provider="anonymous-forbidden-writer", writer_model="anonymous-forbidden-writer-model",
            editor_provider="anonymous-forbidden-editor", editor_model="anonymous-forbidden-editor-model",
        )
        service = self._service(policy)
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        self.expected_shot_count = preview["shot_count"]
        event = service._plan_event(self.token, preview["plan_id"])
        plan = service.services.artifacts.resolve_stage_artifact(
            service._run_id_from_ref(event["plan_ref"]), "script_plan"
        )
        self.assertIn("不得虚构事实", json.loads(plan.path.read_text(encoding="utf-8"))["constraints"]["creative_standard"]["avoid_expressions"][0]["value"])
        with self.assertRaises(FormalGenerationBlocked):
            service.confirm_plan_and_generate(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                plan_id=preview["plan_id"], expected_project_version=3,
            )
        self.assertEqual(len(calls), 2)
        task = service.services.tasks.get_task(self.token, preview["plan_id"])
        self.assertEqual(task["status"], "blocked")
        events = service.services.tasks.list_task_events(self.token, preview["plan_id"])
        self.assertEqual(events[-1]["details"]["provider_calls"], 2)
        parent_run_id = service._run_id_from_ref(event["plan_ref"])
        child_manifests = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.root / "runs" / self.project["project_id"] / "runs").glob(
                "*/run_manifest.json"
            )
            if json.loads(path.read_text(encoding="utf-8")).get("parent_run_id")
            == parent_run_id
        ]
        self.assertEqual(len(child_manifests), 1)
        self.assertEqual(child_manifests[0]["status"], "blocked")
        self.assertEqual(child_manifests[0]["error"]["stage_id"], "script_draft")
        self.assertEqual(
            child_manifests[0]["error"]["code"], "script_draft_contract_blocked"
        )
        self.assertIsNone(ProjectService(self.store).get_project(
            self.token, self.project["project_id"]
        )["current_version_id"])

    def test_plan_primary_event_avoid_hit_blocks_before_draft_or_provider(self):
        project = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名规划禁词项目",
            category_id="launch", expected_workspace_version=self.projects.get_workspace(
                self.token, self.workspace_id
            )["workspace_version"],
        )
        original_project = self.project
        self.project = project
        self._confirmed_brief(
            expected_project_version=0,
            forbidden_content="禁止虚构事实",
            event_titles=["空间建立", "虚构事实发布", "品牌落版"],
        )
        self.project = original_project
        service = self._service()
        cache_runs = self.root / "runs" / project["project_id"] / "runs"
        prior_runs = {path.name for path in cache_runs.iterdir()} if cache_runs.exists() else set()
        with self.assertRaisesRegex(FormalGenerationBlocked, "creative_standard_avoid_expression_hit"):
            service.prepare_plan(
                self.token, workspace_id=self.workspace_id, project_id=project["project_id"],
                expected_project_version=2,
                generation_mode="text_only_draft",
            )
        with self.store.connection() as connection:
            task = connection.execute(
                "SELECT task_id, status FROM tasks WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                (project["project_id"],),
            ).fetchone()
        self.assertEqual(task["status"], "blocked")
        events = TaskService(self.store).list_task_events(self.token, task["task_id"])
        self.assertEqual(events[-1]["details"]["provider_calls"], 0)
        run_id = next(path.name for path in cache_runs.iterdir() if path.name not in prior_runs)
        manifest = RunArtifactResolver(self.root / "runs").read_manifest(run_id)
        self.assertEqual(manifest["stages"]["script_draft"]["attempt"], 0)
        self.assertIsNone(ProjectService(self.store).get_project(
            self.token, project["project_id"]
        )["current_version_id"])

    def test_ascii_avoid_uses_token_boundary_and_cjk_remains_contiguous(self):
        from mvp_v2.planning_core import creative_standard_text_hits

        entries = [{"value": "禁止 AI", "fact_id": "fact-ai", "evidence_refs": ["artifact://evidence/ai"]}]
        self.assertEqual(creative_standard_text_hits(
            entries, [{"shot_no": "1", "text": {"画面描述": "一把 chair 放在舞台中央"}}], fields=("画面描述",),
        ), [])
        self.assertTrue(creative_standard_text_hits(
            entries, [{"shot_no": "1", "text": {"画面描述": "AI 生成的舞台效果"}}], fields=("画面描述",),
        ))
        cjk = [{"value": "禁止虚构事实", "fact_id": "fact-cjk", "evidence_refs": ["artifact://evidence/cjk"]}]
        self.assertTrue(creative_standard_text_hits(
            cjk, [{"shot_no": "1", "text": {"字幕": "请勿虚构，事实必须可追溯"}}], fields=("字幕",),
        ))

    def test_draft_avoid_covers_subtitle_and_copy(self):
        from mvp_v2.planning_core import creative_standard_draft_hits

        constraints = {"creative_standard": {
            "tone_keywords": [], "expression_focus": [],
            "avoid_expressions": [{"value": "禁止虚构事实", "fact_id": "fact-cjk", "evidence_refs": ["artifact://evidence/cjk"]}],
            "references": [],
        }}
        self.assertEqual({hit["field"] for hit in creative_standard_draft_hits(constraints, [{
            "镜号": "1", "画面描述": "正常画面", "声音": "正常声音",
            "字幕": "虚构事实", "文案": "虚构事实",
        }])}, {"字幕", "文案"})

    def test_restart_preview_and_confirm_do_not_bypass_tampered_current_standard(self):
        calls = []
        policy = FormalTextProviderPolicy(
            writer=self._provider("writer", calls), editor=self._provider("editor", calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        )
        service = self._service(policy)
        preview = service.prepare_plan(
            self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
            expected_project_version=2,
            generation_mode="text_only_draft",
        )
        restarted_store = WorkspaceStore(self.store.database_path)
        restarted_briefs = FormalBriefWorkspaceService(restarted_store, artifact_root=self.root / "runtime")
        restarted = self._service(policy, store=restarted_store, briefs=restarted_briefs)
        with restarted_briefs.store.connection() as connection:
            row = connection.execute(
                "SELECT payload_sha256 FROM creative_standard_versions WHERE project_id=?",
                (self.project["project_id"],),
            ).fetchone()
        card = restarted_briefs.creative_standard_artifacts.root / row[0][:2] / f"{row[0]}.json"
        card.write_bytes(card.read_bytes() + b"tampered")
        with self.assertRaises(FormalGenerationBlocked):
            restarted.get_plan_preview(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                plan_id=preview["plan_id"], expected_project_version=3,
            )
        with self.assertRaises(FormalGenerationBlocked):
            restarted.confirm_plan_and_generate(
                self.token, workspace_id=self.workspace_id, project_id=self.project["project_id"],
                plan_id=preview["plan_id"], expected_project_version=3,
            )
        self.assertEqual(calls, [])
        self.assertIsNone(ProjectService(restarted_store).get_project(
            self.token, self.project["project_id"]
        )["current_version_id"])


if __name__ == "__main__":
    unittest.main()
