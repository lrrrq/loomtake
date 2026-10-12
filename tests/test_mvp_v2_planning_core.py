import json
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from jsonschema import validate
from openpyxl import load_workbook

from mvp_v2.generation_stages import read_script_plan, run_script_plan_stage, _formal_planning_events
from mvp_v2.excel_io import ADJACENT_REVIEW_SHEET_NAME, write_template_output_workbook
from mvp_v2.formal_brief_domain import (
    FORMAL_BRIEF_SCHEMA,
    ArtifactPayload,
    ConfirmedBriefInput,
    ConfirmedBriefResolver,
)
from mvp_v2.creative_standard import (
    CONFIRMED_CREATIVE_STANDARD_SCHEMA,
    ConfirmedCreativeStandardInput,
    ConfirmedCreativeStandardResolver,
)
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.loop_runner import (
    _apply_plan_evidence,
    _feedback_structures,
    _generate,
    _merge_brief_plans,
    _record_template_structure_downgrades,
    _structure_conflicts_with_event,
)
from mvp_v2.models import SourceShot
from mvp_v2.pipeline_runtime import open_pipeline_run
from mvp_v2.planning_core import _brief_requested_shot_count, _project_slots, compile_plan_artifact
from mvp_v2.run_contract import sha256_file
from mvp_v2.stage_contract import StageStatus
from mvp_v2.stage_runtime import finish_pipeline_stage, start_pipeline_stage
from mvp_v2.project_service import ProjectService
from mvp_v2.workspace_store import WorkspaceStore


FIXTURE = Path(__file__).parent / "fixtures" / "planning" / "high_density_star_activity.json"


class _Authority:
    def __init__(self, confirmed, raw):
        self.confirmed, self.raw = confirmed, raw

    def load_confirmed(self, value):
        return self.confirmed

    def read_payload_bytes(self, _payload):
        return self.raw


class _CreativeStandardAuthority:
    def __init__(self, confirmed, raw):
        self.confirmed, self.raw = confirmed, raw

    def load_confirmed_creative_standard(self, _value):
        return self.confirmed

    def read_creative_standard_bytes(self, _payload):
        return self.raw


def _formal_confirmed(brief):
    formal_payload = {
        "facts": [
            {
                "fact_id": f"fact_{index}", "field_path": field_name,
                "value_json": json.dumps(value, ensure_ascii=False),
                "certainty": "confirmed",
                "evidence_refs": [{"artifact_ref": "artifact://evidence/anonymous"}],
            }
            for index, (field_name, value) in enumerate(brief.items(), start=1)
            if field_name not in {"Brief来源文件", "Brief来源 sheet", "Brief来源行", "Brief来源 SHA256", "Brief来源证据"}
        ]
    }
    envelope = {
        "schema_version": FORMAL_BRIEF_SCHEMA,
        "workspace_id": "workspace-anonymous",
        "project_id": "project-anonymous",
        "brief_version_id": "brief-v1",
        "brief": formal_payload,
    }
    raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode()
    return (
        ConfirmedBriefInput(
            workspace_id="workspace-anonymous",
            project_id="project-anonymous",
            brief_version_id="brief-v1",
            confirmation_id="confirmation-v1",
            brief_snapshot_id="brief-snapshot-v1",
            payload=ArtifactPayload("artifact://brief/v1.json", sha256(raw).hexdigest()),
            confirmation_status="confirmed",
            unresolved_conflict_count=0,
            confirmed_by_actor_id="actor-anonymous",
            confirmed_at="2026-08-13T00:00:00Z",
        ),
        raw,
    )


def _confirmed_creative_standard(confirmed):
    content = {
        "tone_keywords": [{"value": "克制真实", "fact_id": "tone", "evidence_refs": [{"artifact_ref": "artifact://evidence/anonymous"}]}],
        "expression_focus": [{"value": "突出核心事件", "fact_id": "focus", "evidence_refs": [{"artifact_ref": "artifact://evidence/anonymous"}]}],
        "avoid_expressions": [{"value": "避免夸张承诺", "fact_id": "avoid", "evidence_refs": [{"artifact_ref": "artifact://evidence/anonymous"}]}],
        "references": [],
    }
    content_sha256 = sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    envelope = {
        "workspace_id": confirmed.workspace_id,
        "project_id": confirmed.project_id,
        "creative_standard_version_id": "creative-standard-v1",
        "brief_version_id": confirmed.brief_version_id,
        "brief_payload": confirmed.payload.as_mapping(),
        "content_sha256": content_sha256,
        "content": content,
        "reference_projection": {"missing_fact_ids": []},
    }
    raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    standard = ConfirmedCreativeStandardInput(
        schema_version=CONFIRMED_CREATIVE_STANDARD_SCHEMA,
        workspace_id=confirmed.workspace_id,
        project_id=confirmed.project_id,
        creative_standard_version_id="creative-standard-v1",
        brief_version_id=confirmed.brief_version_id,
        confirmation_id="creative-standard-confirmation-v1",
        brief_snapshot_id=confirmed.brief_snapshot_id,
        brief_payload=confirmed.payload,
        card_payload=ArtifactPayload("artifact://creative-standard/anonymous-v1.json", sha256(raw).hexdigest()),
        confirmation_status="confirmed",
        confirmed_by_actor_id="actor-anonymous",
        confirmed_at="2026-08-13T00:00:00Z",
    )
    return standard, ConfirmedCreativeStandardResolver(_CreativeStandardAuthority(standard, raw))


def _formal_source_manifest():
    digest = "c" * 64
    from mvp_v2.formal_brief_domain import SourceManifest

    return SourceManifest.from_mapping({
        "manifest": {"ref": "artifact://manifest/" + digest, "sha256": digest},
        "documents": [{
            "source_document_id": "anonymous_doc",
            "relative_path": "inputs/anonymous.txt",
            "content_sha256": digest,
            "critical": True,
        }],
    })


def _formal_extracted(project_id, brief):
    evidence_ref = "artifact://evidence/anonymous/page/1"
    facts = [
        {
            "fact_id": f"fact_{index}",
            "field": field_name,
            "value": value,
            "confidence": "direct",
            "evidence_refs": [evidence_ref],
        }
        for index, (field_name, value) in enumerate(brief.items(), start=1)
        if field_name not in {"Brief来源文件", "Brief来源 sheet", "Brief来源行", "Brief来源 SHA256", "Brief来源证据"}
    ]
    return {
        "project_id": project_id,
        "source_documents": [{
            "document_id": "anonymous_doc", "project_id": project_id,
            "filename": "anonymous.txt", "mime_type": "text/plain", "size_bytes": 24,
            "sha256": "c" * 64, "critical": True,
        }],
        "extractions": [{
            "document_id": "anonymous_doc", "status": "succeeded", "critical": True,
            "error": None, "locations": [{
                "evidence_ref": evidence_ref, "location": {"page": 1},
                "text": "匿名正式 Brief",
            }],
        }],
        "images": [], "facts": facts, "conflicts": [],
    }


def _confirmed_workspace(root, brief):
    store = WorkspaceStore(root / "runtime" / "workspace.sqlite3")
    owner = LocalIdentityAdapter(store).bootstrap_owner(
        workspace_name="匿名工作区", display_name="匿名 owner", confirmed=True,
        token="anonymous-owner-token",
    )
    token, workspace_id = owner["auth_token"], owner["workspace_id"]
    project = ProjectService(store).create_project(
        token, workspace_id=workspace_id, name="匿名明星活动", category_id="明星活动",
        expected_workspace_version=0, brief_ref="artifact://brief/" + "a" * 64,
        brief_sha256="a" * 64,
    )
    service = FormalBriefWorkspaceService(store, artifact_root=root / "runtime")
    pending = service.import_frozen(
        token, workspace_id=workspace_id, project_id=project["project_id"],
        source_manifest=_formal_source_manifest(),
        source_artifacts={"anonymous_doc": ArtifactPayload("artifact://source/" + "c" * 64, "c" * 64)},
        extracted=_formal_extracted(project["project_id"], brief), expected_project_version=0,
    )
    confirmed = service.confirm(
        token, workspace_id=workspace_id, project_id=project["project_id"],
        brief_version_id=pending.brief_version_id, confirmed_at="2026-08-13T00:00:00Z",
        expected_project_version=1,
    )
    return store, service, token, workspace_id, project, confirmed


def _slot(index: int, function: str) -> SourceShot:
    return SourceShot(
        script_type="明星活动",
        source_file="anonymous-library.xlsx",
        source_sheet="AI_规范表",
        source_row=index + 10,
        shot_no=str(index),
        structure="开场" if index == 1 else "活动" if index == 2 else "收束",
        duration_seconds="4",
        visual_desc=function,
        shot_function=function,
        visual_subject=function,
        reuse_level="镜头功能可复用",
        slot_index=index,
    )


class PlanningCoreTests(unittest.TestCase):
    def test_strategy_listening_event_uses_information_source_not_venue_source(self):
        venue = SourceShot(
            "发布会", "library.xlsx", "模版", 10,
            structure="开场造势", visual_desc="主会场空镜与舞台光束",
            shot_function="宏大场景建立",
        )
        speech = SourceShot(
            "发布会", "library.xlsx", "模版", 17,
            structure="核心宣讲", visual_desc="演讲者在大屏前发布全球战略与趋势",
            shot_function="核心信息发布",
        )

        projected = _project_slots(
            [venue, speech],
            [[{"event_id": "event_001", "title": "听取全球布局方案以及未来趋势"}]],
        )

        self.assertEqual(projected[0].source_row, 17)

    def test_content_structure_only_changes_for_explicit_feedback_or_clear_conflict(self):
        self.assertTrue(
            _structure_conflicts_with_event("艺人亮相", "情侣隔板挑战", 1)
        )
        self.assertTrue(
            _structure_conflicts_with_event("开场造势", "国内代表发言", 5)
        )
        self.assertFalse(
            _structure_conflicts_with_event("核心宣讲与仪式", "核心宣讲与仪式", 5)
        )
        self.assertEqual(
            _feedback_structures({
                "user_feedback": [{
                    "category": "structure",
                    "shot_no": "2",
                    "desired_change": "结构=氛围环境展示；画面描述=外场空镜",
                }],
            }),
            {"2": "氛围环境展示"},
        )
        audit = {"findings": [], "warning_codes": []}
        _record_template_structure_downgrades(
            audit,
            [{"shot_no": "5", "structure_source": "brief"}],
        )
        self.assertEqual(audit["warning_codes"], ["template_structure_downgraded"])

    def setUp(self):
        self.brief = json.loads(FIXTURE.read_text(encoding="utf-8"))["brief"]
        self.slots = [
            _slot(1, "入场与空间建立"),
            _slot(2, "人物与产品互动"),
            _slot(3, "品牌落版"),
        ]

    def test_high_density_brief_projects_content_defined_n_shots(self):
        plan = compile_plan_artifact(
            self.brief,
            self.slots,
            source_view="ai-spec",
        )

        self.assertEqual(len(plan.template_slots), 18)
        self.assertEqual(len(plan.events), 5)
        self.assertEqual(len(plan.shots), 18)
        self.assertEqual(len(plan.event_graph["edges"]), 4)
        self.assertEqual(sum(row["duration_seconds"] for row in plan.shots), 20)
        self.assertEqual(plan.audit["status"], "pass")
        self.assertTrue(plan.source_status["ai_spec_ready"])
        schema = json.loads(
            (Path(__file__).parents[1] / "schemas" / "plan_artifact_v4.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validate(plan.to_payload(), schema)
        for index, shot in enumerate(plan.shots, start=1):
            self.assertEqual(shot["shot_no"], str(index))
            self.assertGreaterEqual(len(shot["event_ids"]), 1)
            self.assertTrue(shot["brief_evidence"]["text"])
            self.assertTrue(shot["functional_source"]["source_file"])
            self.assertTrue(shot["functional_source"]["source_sheet"])
            self.assertGreater(shot["functional_source"]["source_row"], 0)
            self.assertIn("仅作镜头功能参考", shot["functional_reference_notice"])

    def test_verified_image_context_consumes_all_seven_fields_as_candidates(self):
        image_context = {
            "images": [{
                "business_summary": {
                    "caption": "产品发布现场", "tags": ["发布"], "subjects": ["产品"],
                    "scene": "主舞台", "purpose": "建立发布氛围", "quality": "清晰", "brand": "匿名品牌",
                    "candidate_only": True,
                },
            }],
        }
        plan = compile_plan_artifact(
            self.brief, self.slots, source_view="ai-spec",
            constraints={"project_image_context": image_context},
        )
        self.assertEqual(len(plan.shots), 6)
        self.assertEqual(plan.capacity_report["available_project_images"], 1)
        self.assertEqual(
            plan.capacity_report["hard_slot_quantity_mode"],
            "project_image_capacity",
        )
        for shot in plan.shots:
            candidate = shot["project_image_candidates"][0]
            self.assertEqual(
                set(candidate), {"caption", "tags", "subjects", "scene", "purpose", "quality", "brand"},
            )
            self.assertIn("候选图片业务上下文", shot["project_image_candidate_notice"])
            self.assertIn("Journey6", shot["project_image_candidate_notice"])

    def test_twenty_one_independent_events_expand_without_overload(self):
        brief = dict(self.brief)
        brief["视频时长"] = "90s"
        brief["planning_events"] = [
            {
                "title": f"独立流程事件{index:02d}",
                "evidence": f"Brief第{index:02d}项明确流程",
            }
            for index in range(1, 22)
        ]
        brief["必须出现的内容"] = "、".join(
            f"独立流程事件{index:02d}" for index in range(1, 22)
        )

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertEqual(len(plan.events), 21)
        self.assertGreater(len(plan.shots), 21)
        self.assertEqual(len(plan.template_slots), len(plan.shots))
        self.assertNotIn("hard_template_conflict_unresolved", plan.audit["clarification_codes"])
        self.assertEqual(plan.decision_counts["unresolved_conflicts"], 0)

    def test_v4_hard_slot_quantity_expands_demanded_arc_and_keeps_defaults_elsewhere(self):
        brief = dict(self.brief)
        brief["planning_events"] = [
            {"title": f"粉丝互动{index}", "evidence": f"Brief流程{index}"}
            for index in range(1, 7)
        ]
        brief["必须出现的内容"] = "、".join(
            f"粉丝互动{index}" for index in range(1, 7)
        )

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertEqual([shot["structure"] for shot in plan.shots[:3]], ["氛围造镜"] * 3)
        self.assertEqual(plan.shots[-1]["structure"], "落版")
        self.assertGreater(len(plan.shots), 18)
        self.assertGreaterEqual(
            sum(shot["structure"] == "人气造势" for shot in plan.shots), 6,
        )
        self.assertEqual(plan.decision_counts["unresolved_conflicts"], 0)

    def test_v4_specific_tour_and_performance_actions_use_compatible_arcs(self):
        brief = dict(self.brief)
        brief["planning_events"] = [
            {"title": "参观现场物料装置", "evidence": "Brief流程1"},
            {"title": "Fans Busking 乐队演绎", "evidence": "Brief流程2"},
            {"title": "现场合唱", "evidence": "Brief流程3"},
        ]
        brief["必须出现的内容"] = "参观现场物料装置、Fans Busking 乐队演绎、现场合唱"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")
        structures = {
            shot["primary_event"]: shot["structure"]
            for shot in plan.shots
            if shot["primary_event"] in {
                "参观现场物料装置", "Fans Busking 乐队演绎", "现场合唱",
            }
        }

        self.assertEqual(structures["参观现场物料装置"], "空间巡游")
        self.assertEqual(structures["Fans Busking 乐队演绎"], "核心仪式")
        self.assertEqual(structures["现场合唱"], "核心仪式")

    def test_v4_brand_wrap_is_assigned_to_the_closing_hard_slot(self):
        brief = dict(self.brief)
        brief["planning_events"] = [
            {"title": "产品演示", "evidence": "Brief流程1"},
            {"title": "品牌收束", "evidence": "Brief流程2"},
        ]
        brief["必须出现的内容"] = "产品演示、品牌收束"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertEqual(plan.shots[-1]["primary_event"], "品牌收束")
        self.assertEqual(plan.shots[-1]["structure"], "落版")
        product = next(shot for shot in plan.shots if shot["primary_event"] == "产品演示")
        self.assertNotEqual(product["shot_no"], plan.shots[-1]["shot_no"])

    def test_confirmed_plan_event_does_not_pollute_gap_notes(self):
        script_rows = [{"镜号": "1", "时长(秒)": 1}]
        detail_rows = [{"缺口说明": "原有未解决项"}]
        plan = compile_plan_artifact(
            self.brief,
            self.slots,
            source_view="ai-spec",
        )

        _apply_plan_evidence(script_rows, detail_rows, [plan.shots[0]])

        self.assertEqual(detail_rows[0]["缺口说明"], "原有未解决项")
        self.assertNotIn("Brief规划内容", detail_rows[0]["缺口说明"])
        self.assertEqual(detail_rows[0]["规划主事件"], "入场签到")

    def test_explicit_count_below_event_count_warns_as_flow_compression(self):
        plan = compile_plan_artifact(
            self.brief,
            self.slots,
            source_view="ai-spec",
            constraints={"planned_shot_count": 3},
        )

        self.assertEqual(plan.audit["status"], "needs_clarification")
        self.assertIn("capacity_requires_choice", plan.audit["clarification_codes"])
        self.assertEqual(len(plan.shots), 6)

    def test_production_delivery_clause_does_not_become_event_and_brief_count_is_authoritative(self):
        brief = dict(self.brief)
        brief["视频时长"] = "60s"
        brief["特殊需求"] = "保留旁白字幕"
        brief["交付要求"] = "交付8镜脚本Excel与PDF"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        delivery_clause = "交付8镜脚本Excel与PDF"
        self.assertEqual(len(plan.shots), 8)
        self.assertEqual(plan.capacity_report["requested_shot_count"], 8)
        self.assertEqual(plan.capacity_report["planned_shot_count"], 8)
        self.assertEqual(plan.capacity_report["status"], "ready")
        self.assertNotIn(delivery_clause, json.dumps(plan.events, ensure_ascii=False))
        self.assertNotIn(delivery_clause, json.dumps(plan.shots, ensure_ascii=False))
        production = [
            item for item in plan.requirements
            if item["classification"] == "production_requirement"
        ]
        self.assertEqual(len(production), 2)
        self.assertIn("保留旁白字幕", json.dumps(production, ensure_ascii=False))
        self.assertIn(delivery_clause, json.dumps(production, ensure_ascii=False))

    def test_special_requirements_split_filming_content_from_delivery_clauses(self):
        brief = dict(self.brief)
        brief["特殊需求"] = "保留粉丝合影；字幕展示活动名称；保留产品特写，交付8镜Excel；产品特写，交付8镜Excel"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        special = [item for item in plan.requirements if item["requirement_id"].startswith("special_")]
        content = [item for item in special if item["classification"] != "production_requirement"]
        production = [item for item in special if item["classification"] == "production_requirement"]
        self.assertGreaterEqual(len(content), 4)
        self.assertGreaterEqual(len(production), 2)
        self.assertTrue(any("粉丝合影" in item["text"] for item in content))
        self.assertTrue(any("字幕展示活动名称" in item["text"] for item in content))
        self.assertTrue(any("产品特写" in item["text"] for item in content))
        content_ids = {item["requirement_id"] for item in content}
        self.assertTrue(all(item["shot_nos"] for item in plan.coverage if item["requirement_id"] in content_ids))
        for item in special:
            self.assertTrue(item["evidence_refs"])
            self.assertEqual(item["evidence_refs"][0]["field"], "特殊需求")

    def test_creative_editing_controls_remain_production_requirements_without_becoming_shots(self):
        brief = dict(self.brief)
        controls = ['保留现有四段顺序', '只预览实际需要修改的段落', '保留已保护段落的原文']
        brief['特殊需求'] = '；'.join(controls + ['保留客户交流环节'])
        plan = compile_plan_artifact(brief, self.slots, source_view='ai-spec')
        by_text = {item['text']: item for item in plan.requirements}
        for control in controls:
            self.assertEqual(by_text[control]['classification'], 'production_requirement')
            self.assertTrue(by_text[control]['evidence_refs'])
            self.assertNotIn(control, json.dumps(plan.events, ensure_ascii=False))
            self.assertNotIn(control, json.dumps(plan.shots, ensure_ascii=False))
        self.assertEqual(by_text['保留客户交流环节']['classification'], 'mandatory_content')

    def _rounds_eight_block_plan(self, *, mandatory="", special="", flow="", visuals=None):
        visuals = visuals or [
            "发布会场地外景，嘉宾进入会场。",
            "米白色随行杯杯身与同色杯盖特写，杯身印有匿名品牌。",
            "嘉宾签到并领取资料。",
            "主持人站在舞台中央介绍新品。",
            "演示人员轻放杯盖并展示杯口。",
            "来宾手持随行杯体验使用。",
            "嘉宾合影留念。",
            "产品静置桌面，片尾显示“带上随行杯，轻松出发”。",
        ]
        brief = {
            "项目名称": "匿名随行杯发布会", "脚本类型": "发布会",
            "品牌或客户": "匿名品牌", "视频目标": "介绍新品并回顾发布活动",
            "使用场景": "新品发布会", "视频时长": "60s", "风格调性": "自然明亮",
            "必须出现的内容": mandatory, "特殊需求": special,
            "事件流程": flow,
        }
        if flow:
            brief["planning_events"] = _formal_planning_events(flow, fact_id="flow_fact",
                evidence_refs=["artifact://evidence/anonymous-flow"])
        blocks = [{"block_id": f"block_{index}", "fields": {
            "段落": f"环节{index}", "画面描述": visual,
            "声音": "轻快木吉他配乐，无旁白。", "时长": f"{seconds}秒",
        }, "source_refs": [{"source_file": "anonymous-brief.txt",
            "source_sheet": "正文", "source_row": index}]}
            for index, (visual, seconds) in enumerate(zip(visuals, (8, 7, 7, 8, 8, 7, 8, 7)), start=1)]
        return compile_plan_artifact(brief, self.slots, source_view="ai-spec", constraints={
            "creative_draft_binding": {
                "workspace_id": "workspace-anonymous", "project_id": "project-anonymous",
                "draft_id": "anonymous-draft", "draft_revision": 4,
                "draft_sha256": "a" * 64,
                "payload_ref": "artifact://anonymous-creative-input", "payload_sha256": "b" * 64,
                "protected_block_ids": ["block_1"], "blocks": blocks, "image_references": [],
            },
        })

    def test_rounds_eight_blocks_and_audio_constraints_do_not_create_added_shots(self):
        controls = ("8个镜头", "轻快木吉他配乐", "无旁白", "少字幕")
        plan = self._rounds_eight_block_plan(special="；".join(controls))
        self.assertEqual(plan.capacity_report["requested_shot_count"], 8)
        self.assertEqual(len(plan.shots), 8)
        self.assertEqual(sum(row["duration_seconds"] for row in plan.shots), 60)
        for index, shot in enumerate(plan.shots, start=1):
            self.assertIn(f"creative_block_{index}", shot["event_ids"])
        by_text = {row["text"]: row for row in plan.requirements}
        for control in controls:
            self.assertIn(by_text[control]["classification"], {"production_requirement", "style_preference"})
            self.assertFalse(any(control == event["text"] for event in plan.events))

    def test_preserved_flow_and_static_objects_reuse_eight_accepted_blocks(self):
        plan = self._rounds_eight_block_plan(
            mandatory="米白色直筒随行杯；同色杯盖；匿名品牌字样；白色签到台；浅木色展示桌；浅绿色背景板",
            special="8个镜头；无旁白；少字幕；结尾字幕“轻松出发”",
            flow="门口签到、产品陈列、主持开场、负责人介绍、来宾体验、合影收尾",
            visuals=[
                "明亮入口，两位来宾在白色签到台前签名，浅绿色背景板在后方。",
                "产品陈列：米白色直筒随行杯、同色杯盖，匿名品牌字样清晰，浅木色展示桌与浅绿色背景板入画。",
                "主持开场：主持人欢迎来宾。", "负责人介绍：负责人介绍新品。",
                "产品细节：演示人员轻放杯盖。", "来宾体验：来宾拿起随行杯。",
                "合影收尾：来宾合影。", "产品静置，结尾字幕“轻松出发”。",
            ],
        )
        self.assertEqual(len(plan.shots), 8)
        self.assertEqual(sum(row["duration_seconds"] for row in plan.shots), 60)
        self.assertEqual(plan.audit["blocking_codes"], [])
        self.assertFalse(plan.conflicts)
        by_id = {row["requirement_id"]: row for row in plan.requirements}
        for row in plan.coverage:
            self.assertEqual(row["status"], "covered", by_id[row["requirement_id"]]["text"])
            if row["kind"] == "production_requirement":
                self.assertEqual(row["shot_nos"], [])

    def test_arrow_flow_and_normal_synonyms_keep_eight_accepted_blocks(self):
        plan = self._rounds_eight_block_plan(
            mandatory="米白色直筒杯、同色杯盖、杯身写匿名；片尾完整字幕：“轻松出发”",
            special="总共8镜、60秒。轻快音乐，无旁白，少字幕。参考图仅说明景别、机位和布置。",
            flow="签到→产品与现场布置→主持开场→负责人介绍→来宾试用→合影收尾",
            visuals=["门口签到：来宾在白色签到台前签到。",
                     "产品和现场布置：米白色直筒杯、同色杯盖，杯身写匿名。",
                     "主持人开场：主持人欢迎来宾。", "负责人介绍杯子：负责人拿起杯子介绍。",
                     "大家拿起试用：来宾手持杯子试用并交流。", "杯子使用细节：杯盖轻放。",
                     "轻松氛围：来宾走动。", "合影收尾：大家合影，字幕“轻松出发”。"],
        )
        self.assertEqual(len(plan.shots), 8)
        self.assertEqual(sum(row["duration_seconds"] for row in plan.shots), 60)
        self.assertFalse(plan.conflicts)
        self.assertEqual(plan.audit["blocking_codes"], [])
        self.assertEqual([event["title"] for event in _formal_planning_events('签到→片尾“向前→出发”',
            fact_id="flow", evidence_refs=["artifact://evidence/flow"])], ["签到", '片尾“向前→出发”'])

    def test_rounds_literal_closing_copy_stays_whole_and_covered_by_existing_last_block(self):
        copy = '片尾字幕“带上随行杯，轻松出发”'
        plan = self._rounds_eight_block_plan(special="8个镜头；" + copy)
        self.assertEqual(len(plan.shots), 8)
        captions = [row for row in plan.requirements if row["original_text"] == copy]
        self.assertEqual(len(captions), 1)
        coverage = next(row for row in plan.coverage if row["requirement_id"] == captions[0]["requirement_id"])
        self.assertEqual(coverage["shot_nos"], ["8"])
        self.assertFalse(any(row.get("status") == "unresolved" for row in plan.conflicts))
        self.assertNotIn("mandatory_fact_pending", plan.audit["blocking_codes"])

    def test_unquoted_caption_colon_preserves_comma_and_next_separate_requirement(self):
        from mvp_v2.planning_core import _split_requirement_text
        self.assertEqual(_split_requirement_text('杯身写匿名；片尾完整字幕：带上匿名，轻松出发；国内代表1发言'), [
            '杯身写匿名', '片尾完整字幕：带上匿名，轻松出发', '国内代表1发言',
        ])
        self.assertEqual(_split_requirement_text('字幕：“向前，出发”，国内代表1发言'), [
            '字幕：“向前，出发”', '国内代表1发言',
        ])

    def test_rounds_covered_cup_attributes_share_the_original_product_shot(self):
        attributes = ("米白色杯身", "同色杯盖", "杯身印有匿名品牌")
        plan = self._rounds_eight_block_plan(mandatory="；".join(attributes), special="8个镜头")
        self.assertEqual(len(plan.shots), 8)
        for attribute in attributes:
            requirement = next(row for row in plan.requirements if row["text"] == attribute and row["mandatory"])
            coverage = next(row for row in plan.coverage if row["requirement_id"] == requirement["requirement_id"])
            self.assertEqual(coverage["shot_nos"], ["2"], attribute)
        self.assertEqual(plan.capacity_report["status"], "ready")

    def test_rounds_missing_representatives_are_not_covered_by_one_creative_block(self):
        plan = self._rounds_eight_block_plan(
            mandatory="国内代表1发言（全景+中景）；国内代表2发言（全景+中景）；国外代表3发言（全景+中景）",
            special="8个镜头",
        )
        # These actions are absent from the accepted eight blocks. They must
        # remain separate requirements rather than pretending cup attributes
        # or one existing person can prove all three speakers were covered.
        representatives = [row for row in plan.requirements if row["mandatory"] and "代表" in row["text"]]
        self.assertEqual(len(representatives), 3)
        covered = {row["requirement_id"]: row["shot_nos"] for row in plan.coverage}
        nonempty = [tuple(covered[row["requirement_id"]]) for row in representatives if covered[row["requirement_id"]]]
        self.assertEqual(len(nonempty), len(set(nonempty)))
        self.assertTrue(plan.conflicts or len(nonempty) == 3)

    def test_output_count_in_mixed_delivery_clause_is_authoritative(self):
        brief = dict(self.brief)
        brief["视频时长"] = "60s"
        brief["交付要求"] = "拍摄 8 镜并输出 8 镜脚本 Excel 与 PDF"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertEqual(plan.capacity_report["requested_shot_count"], 8)
        self.assertEqual(plan.capacity_report["planned_shot_count"], 8)
        self.assertEqual(plan.capacity_report["status"], "ready")
        self.assertFalse(any("拍摄" in str(event) for event in plan.events))
        self.assertTrue(all(item["classification"] == "production_requirement" for item in plan.requirements if item["requirement_id"].startswith("delivery_")))

    def test_approximate_count_requires_one_confirmed_target(self):
        brief = dict(self.brief)
        brief["交付要求"] = "输出约 8 镜脚本 Excel 与 PDF"

        requested, issue = _brief_requested_shot_count(brief)
        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertIsNone(requested)
        self.assertEqual(issue, "approximate_brief_shot_count")
        self.assertEqual(plan.capacity_report["planned_shot_count"], 18)
        self.assertEqual(plan.audit["status"], "needs_clarification")
        self.assertIn("approximate_brief_shot_count", plan.audit["clarification_codes"])

    def test_shot_reference_and_duration_are_not_total_shot_declarations(self):
        brief = dict(self.brief)
        brief["必须出现的内容"] = "第8镜展示产品；单镜时长 8 秒"
        brief["交付要求"] = "保留第8镜图片来源证据"

        requested, issue = _brief_requested_shot_count(brief)

        self.assertIsNone(requested)
        self.assertEqual(issue, "")

    def test_production_and_filming_verbs_share_explicit_shot_count_contract(self):
        for text in ("制作 8 镜脚本", "拍摄 8 镜素材"):
            with self.subTest(text=text):
                requested, issue = _brief_requested_shot_count({"交付要求": text})
                self.assertEqual((requested, issue), (8, ""))

    def test_unknown_event_fact_requires_clarification(self):
        brief = dict(self.brief)
        brief["planning_events"] = [
            {"title": "待确认艺人致辞", "evidence": "艺人是否致辞待确认"},
            {"title": "品牌落版", "evidence": "Brief明确要求品牌落版"},
        ]

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertEqual(plan.audit["status"], "needs_clarification")
        self.assertIn("fact_missing", plan.audit["clarification_codes"])
        self.assertEqual(plan.events[0]["fact_status"], "pending_confirmation")

    def test_requirement_coverage_and_finding_contract_are_complete(self):
        brief = dict(self.brief)
        brief["必须出现的内容"] = "品牌落版、产品互动"
        brief["禁忌或不能出现的内容"] = "不得虚构嘉宾身份"
        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        mandatory = [
            item for item in plan.requirements if item["mandatory"]
        ]
        self.assertEqual(len(mandatory), 3)
        self.assertTrue(all(item["evidence_refs"] for item in mandatory))
        coverage = {item["requirement_id"]: item for item in plan.coverage}
        self.assertTrue(all(coverage[item["requirement_id"]]["shot_nos"] for item in mandatory))
        self.assertTrue(all(
            set(("code", "severity", "message", "requirement_ref", "risk", "cannot_downgrade_reason")) <= set(item)
            for item in plan.audit["findings"]
        ))

    def test_v3_position_contract_adds_an_authoritative_opening_shot(self):
        brief = dict(self.brief)
        brief["视频时长"] = "24s"
        brief["planning_events"] = [
            {"title": "情侣隔板挑战", "evidence": "Brief流程1"},
            {"title": "回答一致后撤掉隔板", "evidence": "Brief流程2"},
            {"title": "领取品牌礼物", "evidence": "Brief流程3"},
        ]
        brief["必须出现的内容"] = "情侣隔板挑战、回答一致后撤掉隔板、领取品牌礼物"
        brief["特殊需求"] = "开头需要AI蝴蝶从KV花园飞出，落到现场空镜中做转场"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        opening = next(
            item for item in plan.requirements
            if item["classification"] == "position_constraint"
        )
        coverage = next(
            item for item in plan.coverage
            if item["requirement_id"] == opening["requirement_id"]
        )
        trace = next(
            item for item in plan.traceability
            if item["requirement_id"] == opening["requirement_id"]
        )
        self.assertEqual(plan.plan_schema_version, "mplus-plan-artifact/v4")
        self.assertEqual(opening["position_rule"]["type"], "opening")
        self.assertEqual(coverage["shot_nos"], ["1"])
        self.assertEqual(plan.shots[0]["primary_event_id"], opening["requirement_id"])
        self.assertIn("AI蝴蝶", plan.shots[0]["primary_event"])
        self.assertEqual(trace["plan_shot_nos"], ["1"])
        self.assertEqual(len(plan.events), 3)
        self.assertEqual(len(plan.shots), 18)
        self.assertEqual(plan.audit["status"], "pass")

    def test_v3_position_contract_moves_matching_event_without_duplicate_opening(self):
        brief = dict(self.brief)
        brief["视频时长"] = "24s"
        brief["planning_events"] = [
            {"title": "现场空镜", "evidence": "Brief流程1"},
            {"title": "AI蝴蝶从KV花园飞出并落到现场", "evidence": "Brief流程2"},
            {"title": "情侣隔板挑战", "evidence": "Brief流程3"},
            {"title": "品牌落版", "evidence": "Brief流程4"},
        ]
        brief["必须出现的内容"] = "情侣隔板挑战、品牌落版"
        brief["特殊需求"] = "开头需要AI蝴蝶从KV花园飞出，落到现场中做转场"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        butterfly_shots = [
            shot for shot in plan.shots if "AI蝴蝶" in shot["primary_event"]
        ]
        self.assertEqual(len(plan.shots), 18)
        self.assertEqual(len(butterfly_shots), 1)
        self.assertEqual(butterfly_shots[0]["shot_no"], "1")
        self.assertNotIn("position_contract_violated", plan.audit["blocking_codes"])

    def test_v3_position_contract_does_not_relabel_a_location_event_as_ai_opening(self):
        brief = dict(self.brief)
        brief["视频时长"] = "24s"
        brief["planning_events"] = [
            {"title": "参观会展中心现场物料装置", "evidence": "Brief流程1"},
            {"title": "启动亮灯仪式", "evidence": "Brief流程2"},
        ]
        brief["特殊需求"] = "开头AI从云层穿过，飞行视角落到会展中心外立面"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertIn("云层", plan.shots[0]["primary_event"])
        location = next(
            shot for shot in plan.shots
            if shot["primary_event"] == "参观会展中心现场物料装置"
        )
        self.assertGreaterEqual(int(location["shot_no"]), 4)

    def test_v3_multiple_independent_mandatory_details_do_not_collapse_into_one_flow(self):
        brief = dict(self.brief)
        brief["视频时长"] = "24s"
        brief["planning_events"] = [
            {"title": "全球各代表发言讲话", "evidence": "Brief流程1"},
            {"title": "品牌落版", "evidence": "Brief流程2"},
        ]
        brief["必须出现的内容"] = "国内代表1发言（全景+中景）；国内代表2发言（全景+中景）；国外代表3发言（全景+中景）"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        detail_shots = {
            shot_no
            for item in plan.traceability
            if item["classification"] == "mandatory_content"
            for shot_no in item["plan_shot_nos"]
        }
        self.assertEqual(len(detail_shots), 3)

    def test_v3_template_selection_is_event_led_even_when_counts_match(self):
        brief = dict(self.brief)
        brief["视频时长"] = "8s"
        brief["planning_events"] = [
            {"title": "茶歇交流", "evidence": "Brief流程1"},
            {"title": "品牌落版", "evidence": "Brief流程2"},
        ]
        brief["必须出现的内容"] = "茶歇交流、品牌落版"
        slots = [
            _slot(1, "品牌落版与主视觉收束"),
            _slot(2, "茶歇交流与服务细节"),
        ]

        plan = compile_plan_artifact(brief, slots, source_view="ai-spec")

        tea = next(shot for shot in plan.shots if shot["primary_event"] == "茶歇交流")
        closing = next(shot for shot in plan.shots if shot["primary_event"] == "品牌落版")
        self.assertNotEqual(tea["shot_no"], closing["shot_no"])
        self.assertEqual(plan.shots[-1]["structure"], "落版")

    def test_v3_dynamic_star_plan_preserves_template_opening_structure_arc(self):
        brief = dict(self.brief)
        brief["视频时长"] = "60s"
        brief["planning_events"] = [
            {"title": f"流程{index}", "evidence": f"Brief流程{index}"}
            for index in range(1, 17)
        ]
        slots = [
            SourceShot(
                script_type="明星活动", source_file="star.xlsx", source_sheet="模版",
                source_row=index + 6, shot_no=str(index), slot_index=index,
                structure=(
                    "氛围造镜" if index <= 3 else "空间巡游" if index <= 7
                    else "人气造势" if index <= 10 else "艺人亮相" if index <= 13
                    else "核心仪式" if index <= 17 else "落版"
                ),
                shot_function=f"功能{index}", visual_subject=f"主体{index}",
            )
            for index in range(1, 19)
        ]

        plan = compile_plan_artifact(brief, slots, source_view="ai-spec")

        self.assertEqual([shot["structure"] for shot in plan.shots[:3]], ["氛围造镜"] * 3)
        self.assertEqual(plan.shots[-1]["structure"], "落版")
        self.assertTrue(all(
            shot["structure_source"] in {"structural_required", "conflict"}
            for shot in plan.shots[:3]
        ))

    def test_v4_default_hard_slot_count_and_short_duration_scaling(self):
        default = compile_plan_artifact(
            self.brief, self.slots, source_view="ai-spec",
        )
        short_brief = dict(self.brief)
        short_brief["视频时长"] = "8s"
        short = compile_plan_artifact(
            short_brief, self.slots, source_view="ai-spec",
        )

        self.assertEqual(len(default.shots), 18)
        self.assertEqual([shot["structure"] for shot in default.shots[:3]], ["氛围造镜"] * 3)
        self.assertNotIn("核心仪式", [shot["structure"] for shot in default.shots])
        placeholder_titles = [
            shot["primary_event"] for shot in default.shots
            if str(shot["primary_event_id"]).startswith("template_slot_")
        ]
        self.assertEqual(len(placeholder_titles), len(set(placeholder_titles)))
        self.assertEqual(len(short.shots), 6)
        self.assertEqual(
            [shot["structure"] for shot in short.shots],
            ["氛围造镜", "氛围造镜", "空间巡游", "人气造势", "艺人亮相", "落版"],
        )

    def test_v4_core_speech_moves_after_atmosphere_without_position_contract(self):
        brief = dict(self.brief)
        brief["planning_events"] = [{"title": "核心发言", "evidence": "Brief流程"}]
        brief["必须出现的内容"] = "核心发言"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        speech = next(shot for shot in plan.shots if shot["primary_event"] == "核心发言")
        self.assertGreaterEqual(int(speech["shot_no"]), 4)
        self.assertEqual(plan.decision_counts["unresolved_conflicts"], 0)
        generated_core = [
            shot for shot in plan.shots
            if shot["structure"] == "核心仪式" and shot["generated_structure_only"]
        ]
        self.assertTrue(all(
            shot["primary_event"].startswith("核心节奏·") for shot in generated_core
        ))

    def test_v4_opening_core_speech_conflicts_with_hard_atmosphere(self):
        brief = dict(self.brief)
        brief["planning_events"] = [{"title": "核心发言", "evidence": "Brief流程"}]
        brief["必须出现的内容"] = "核心发言"
        brief["特殊需求"] = "开头核心发言"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertEqual(plan.audit["status"], "needs_clarification")
        self.assertEqual(plan.decision_counts["unresolved_conflicts"], 1)
        self.assertEqual(plan.conflicts[0]["template_value"], "氛围造镜")
        self.assertEqual(plan.conflicts[0]["brief_value"], "开头核心发言")

    def test_v4_technique_default_override_is_explained_without_conflict(self):
        brief = dict(self.brief)
        brief["planning_events"] = [{
            "title": "核心发言", "evidence": "Brief流程", "camera_move": "环绕",
        }]
        brief["必须出现的内容"] = "核心发言"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")
        speech = next(shot for shot in plan.shots if shot["primary_event"] == "核心发言")
        camera = next(
            item for item in speech["field_decisions"] if item["field"] == "camera_move"
        )

        self.assertEqual(camera["final_value"], "环绕")
        self.assertEqual(camera["decision_status"], "overridden")
        self.assertEqual(camera["override_reason_code"], "brief_precedence_over_technique_default")
        self.assertEqual(plan.decision_counts["unexplained_overrides"], 0)
        self.assertEqual(plan.decision_counts["unresolved_conflicts"], 0)

    def test_v4_example_only_fields_are_deferred_not_project_facts(self):
        plan = compile_plan_artifact(self.brief, self.slots, source_view="ai-spec")
        decisions = {
            item["field"]: item for item in plan.shots[0]["field_decisions"]
        }

        self.assertEqual(decisions["visual_description"]["value_status"], "deferred")
        self.assertEqual(decisions["reference_image"]["selected_source"], "deferred")
        self.assertEqual(decisions["visual_description"]["final_value"], "")
        self.assertEqual(len(decisions), 11)

    def test_v3_unmatched_mandatory_content_gets_capacity_not_round_robin(self):
        brief = dict(self.brief)
        brief["视频时长"] = "16s"
        brief["planning_events"] = [
            {"title": "嘉宾签到", "evidence": "Brief流程1"},
            {"title": "品牌落版", "evidence": "Brief流程2"},
        ]
        brief["必须出现的内容"] = "机器狗巡游"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        requirement = next(
            item for item in plan.requirements
            if item["classification"] == "mandatory_content"
        )
        bound = [
            shot for shot in plan.shots
            if requirement["requirement_id"] in shot.get("requirement_ids", [])
        ]
        self.assertEqual(len(plan.shots), 6)
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["primary_event_id"], requirement["requirement_id"])
        self.assertIn("机器狗", bound[0]["primary_event"])

    def test_v3_review_preference_requires_confirmation_before_binding(self):
        proposal = {
            "requirement_id": "review_opening_empty",
            "text": "开头应该是空镜展示",
            "evidence_refs": [{"type": "review_workbook", "source": {"file": "review.xlsx", "sheet": "复核", "row": 2}}],
        }
        pending = compile_plan_artifact(
            self.brief, self.slots, source_view="ai-spec",
            constraints={"review_requirement_proposals": [proposal]},
        )
        confirmed = compile_plan_artifact(
            self.brief, self.slots, source_view="ai-spec",
            constraints={"confirmed_review_requirements": [proposal]},
        )

        pending_trace = next(
            item for item in pending.traceability
            if item["requirement_id"] == proposal["requirement_id"]
        )
        confirmed_requirement = next(
            item for item in confirmed.requirements
            if item["requirement_id"] == proposal["requirement_id"]
        )
        self.assertEqual(pending.audit["status"], "needs_clarification")
        self.assertEqual(pending_trace["final_status"], "review_proposed")
        self.assertEqual(pending_trace["plan_shot_nos"], [])
        self.assertEqual(confirmed_requirement["origin"], "confirmed_review")
        self.assertEqual(confirmed_requirement["position_rule"]["type"], "opening")
        self.assertEqual(confirmed.coverage[-1]["shot_nos"], ["1"])

    def test_v3_confirmed_shot_review_is_an_overlay_not_an_extra_event(self):
        review = {
            "requirement_id": "review_shot_2",
            "text": "人物到场与上一镜重复，改为已在场内互动",
            "shot_no": "2",
            "evidence_refs": [{"type": "review_workbook"}],
        }

        baseline = compile_plan_artifact(
            self.brief, self.slots, source_view="ai-spec",
        )
        plan = compile_plan_artifact(
            self.brief, self.slots, source_view="ai-spec",
            constraints={"confirmed_review_requirements": [review]},
        )

        traced = next(
            item for item in plan.traceability
            if item["requirement_id"] == review["requirement_id"]
        )
        self.assertEqual(len(plan.shots), len(baseline.shots))
        self.assertEqual(traced["plan_shot_nos"], ["2"])
        self.assertEqual(traced["final_status"], "review_confirmed")
        self.assertEqual(len(traced["review_findings"]), 1)

    def test_v3_capacity_shortage_is_clarification_not_silent_overwrite(self):
        brief = dict(self.brief)
        brief["视频时长"] = "2s"
        brief["planning_events"] = [
            {"title": "签到", "evidence": "Brief流程1"},
            {"title": "发言", "evidence": "Brief流程2"},
            {"title": "落版", "evidence": "Brief流程3"},
        ]

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertEqual(plan.capacity_report["status"], "needs_clarification")
        self.assertEqual(plan.audit["status"], "needs_clarification")
        self.assertIn("capacity_requires_choice", plan.audit["clarification_codes"])

    def test_v3_multi_brief_merge_namespaces_trace_and_requirement_bindings(self):
        first = compile_plan_artifact(
            self.brief, self.slots, source_view="ai-spec",
        ).to_payload()
        second = compile_plan_artifact(
            self.brief, self.slots, source_view="ai-spec",
        ).to_payload()

        merged = _merge_brief_plans([first, second])

        ids = [item["requirement_id"] for item in merged["traceability"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(value.startswith("brief_") for value in ids))
        referenced = {
            requirement_id
            for shot in merged["shot_plans"]
            for requirement_id in shot.get("requirement_ids") or []
        }
        self.assertTrue(referenced <= set(ids))

    def test_explicit_duration_budget_mismatch_is_blocked(self):
        brief = dict(self.brief)
        brief["planning_events"] = [
            {"title": "入场签到", "evidence": "Brief明确", "duration_seconds": 3},
            {"title": "品牌落版", "evidence": "Brief明确", "duration_seconds": 3},
        ]

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertEqual(plan.audit["status"], "needs_clarification")
        self.assertIn("capacity_requires_choice", plan.audit["clarification_codes"])

    def test_explicit_event_budget_is_preserved_across_multiple_shots(self):
        brief = dict(self.brief)
        brief["视频时长"] = "12s"
        brief["planning_events"] = [
            {
                "title": "产品互动",
                "evidence": "Brief明确",
                "duration_seconds": 6,
                "shot_units": 2,
            },
            {
                "title": "品牌落版",
                "evidence": "Brief明确",
                "duration_seconds": 6,
                "shot_units": 1,
            },
        ]
        brief["必须出现的内容"] = "产品互动、品牌落版"

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertEqual(len(plan.shots), 6)
        self.assertEqual(sum(shot["duration_seconds"] for shot in plan.shots), 12)
        self.assertEqual(plan.audit["status"], "pass")

    def test_declared_event_order_inversion_is_blocked(self):
        brief = dict(self.brief)
        brief["planning_events"] = [
            {"title": "品牌落版", "evidence": "Brief明确", "order": 2},
            {"title": "入场签到", "evidence": "Brief明确", "order": 1},
        ]

        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec")

        self.assertEqual(plan.audit["status"], "blocked")
        self.assertIn("event_order_inconsistent", plan.audit["blocking_codes"])

    def test_unconfirmed_source_mechanism_is_explicitly_excluded(self):
        source = _slot(1, "剪彩与高管致辞")
        brief = dict(self.brief)
        brief["视频时长"] = "8s"
        brief["planning_events"] = [
            {"title": "品牌落版", "evidence": "Brief仅确认品牌落版"}
        ]

        plan = compile_plan_artifact(brief, [source], source_view="ai-spec")

        self.assertEqual(plan.audit["status"], "pass")
        self.assertIn("source_mechanism_excluded", plan.audit["warning_codes"])
        self.assertEqual(
            plan.shots[0]["excluded_source_mechanisms"],
            ["剪彩", "高管致辞"],
        )

    def test_v1_script_plan_remains_readable(self):
        payload = {
            "schema_version": "mplus-script-plan/v1",
            "briefs": [self.brief],
            "brief": self.brief,
            "constraints": {},
            "source_view": "human",
            "source_refs": [{
                "source_file": "anonymous-library.xlsx",
                "source_sheet": "模版",
                "source_row": 11,
            }],
            "script_rows": [{"镜号": "1", "结构": "开场"}],
            "detail_rows": [{
                "采用来源文件": "anonymous-library.xlsx",
                "采用来源 sheet": "模版",
                "采用来源行": 11,
            }],
            "decisions": [],
            "template_slots": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script_plan.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            artifact = read_script_plan(path)

        self.assertEqual(artifact.schema_version, "mplus-script-plan/v1")
        self.assertEqual(artifact.events, [])
        self.assertEqual(artifact.shot_plans, [])
        self.assertEqual(artifact.planning_audit, {})

    def test_real_script_plan_path_compiles_v2_and_keeps_all_n_contracts_aligned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            confirmed, raw = _formal_confirmed(self.brief)
            standard, standard_resolver = _confirmed_creative_standard(confirmed)
            with open_pipeline_run(
                self.brief,
                root / "assets",
                root / "source",
                cache_fingerprint="planning-fixture",
            ) as state:
                self._complete_prerequisites(state)
                with patch(
                    "mvp_v2.loop_runner.resolve_script_type_workbook",
                    return_value=root / "anonymous-library.xlsx",
                ), patch(
                    "mvp_v2.loop_runner.load_ai_spec_shots",
                    return_value=(self.slots, []),
                ), patch(
                    "mvp_v2.loop_runner.compile_plan_artifact",
                    wraps=compile_plan_artifact,
                ) as compile_plan:
                    artifact = run_script_plan_stage(
                        state,
                        briefs=[],
                        constraints={},
                        source_view="ai-spec",
                        builder=lambda planning_briefs: _generate(
                            planning_briefs, root, {}, None, "ai-spec"
                        ),
                        confirmed_brief_input=confirmed,
                        confirmed_brief_resolver=ConfirmedBriefResolver(
                            _Authority(confirmed, raw)
                        ),
                        confirmed_creative_standard_input=standard,
                        confirmed_creative_standard_resolver=standard_resolver,
                    )
                    compile_plan.assert_called_once()
                output = root / "anonymous-plan.xlsx"
                write_template_output_workbook(
                    output,
                    artifact.script_rows,
                    artifact.detail_rows,
                    [],
                )
                workbook = load_workbook(output, data_only=True)
                adjacent_count = workbook[ADJACENT_REVIEW_SHEET_NAME].max_row - 1
                workbook.close()

            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
            plan_payload = json.loads(artifact.path.read_text(encoding="utf-8"))
        self.assertEqual(artifact.schema_version, "mplus-script-plan/v4")
        self.assertEqual(artifact.plan_schema_version, "mplus-plan-artifact/v4")
        self.assertEqual(artifact.planning_audit["status"], "pass")
        self.assertEqual(len(artifact.script_rows), 18)
        self.assertEqual(len(artifact.detail_rows), 18)
        self.assertEqual(len(artifact.decisions), 18)
        self.assertEqual(len(artifact.template_slots), 18)
        self.assertEqual(len(artifact.shot_plans), 18)
        self.assertEqual(artifact.confirmed_brief_input["project_id"], confirmed.project_id)
        self.assertEqual(adjacent_count, len(artifact.script_rows) - 1)
        self.assertEqual(
            plan_payload["confirmed_brief_provenance"]["brief_version_id"],
            confirmed.brief_version_id,
        )
        self.assertEqual(
            plan_payload["confirmed_brief_provenance"]["payload_sha256"],
            confirmed.payload.sha256,
        )
        self.assertTrue(plan_payload["confirmed_brief_provenance"]["facts"])
        self.assertEqual(manifest["stages"]["script_plan"]["status"], "succeeded")
        self.assertEqual(manifest["stages"]["script_draft"]["attempt"], 0)

    def test_workspace_confirmed_brief_restarts_into_v2_plan_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, _service, _token, _workspace_id, _project, confirmed = _confirmed_workspace(root, self.brief)
            restarted = FormalBriefWorkspaceService(
                WorkspaceStore(store.database_path), artifact_root=root / "runtime"
            )
            with open_pipeline_run(
                {"项目名称": "匿名明星活动"}, root / "assets", root / "source",
                cache_fingerprint="formal-workspace-v2",
            ) as state:
                self._complete_prerequisites(state)
                with patch("mvp_v2.loop_runner.resolve_script_type_workbook", return_value=root / "anonymous-library.xlsx"), patch("mvp_v2.loop_runner.load_ai_spec_shots", return_value=(self.slots, [])):
                    artifact = run_script_plan_stage(
                        state, briefs=[], constraints={}, source_view="ai-spec",
                        builder=lambda planning_briefs: _generate(planning_briefs, root, {}, None, "ai-spec"),
                        confirmed_brief_input=confirmed,
                        confirmed_brief_resolver=restarted.resolver(),
                        confirmed_creative_standard_input=restarted.resolve_current_paired(
                            _token, workspace_id=_workspace_id, project_id=_project["project_id"]
                        ).creative_standard,
                        confirmed_creative_standard_resolver=restarted.creative_standard_resolver(),
                    )
                output = root / "anonymous-plan.xlsx"
                write_template_output_workbook(output, artifact.script_rows, artifact.detail_rows, [])
                workbook = load_workbook(output, data_only=True)
                adjacent_count = workbook[ADJACENT_REVIEW_SHEET_NAME].max_row - 1
                workbook.close()
            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(artifact.schema_version, "mplus-script-plan/v4")
        self.assertEqual(artifact.planning_audit["status"], "pass")
        self.assertEqual(len(artifact.script_rows), 18)
        self.assertEqual(len(artifact.detail_rows), len(artifact.decisions))
        self.assertEqual(len(artifact.script_rows), len(artifact.template_slots))
        self.assertEqual(len(artifact.script_rows), len(artifact.shot_plans))
        self.assertEqual(adjacent_count, len(artifact.script_rows) - 1)
        self.assertEqual(manifest["stages"]["script_draft"]["attempt"], 0)

    def test_workspace_resolver_blocks_unconfirmed_conflict_cross_project_and_tamper(self):
        cases = ("unconfirmed", "conflict", "cross_project", "tamper")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store, service, _token, workspace_id, project, confirmed = _confirmed_workspace(root, self.brief)
                if case == "unconfirmed":
                    input_value = replace(confirmed, confirmation_status="pending")
                elif case == "conflict":
                    input_value = replace(confirmed, unresolved_conflict_count=1)
                elif case == "cross_project":
                    input_value = replace(confirmed, project_id="other-project")
                else:
                    target = service.artifacts.root / confirmed.payload.sha256[:2] / f"{confirmed.payload.sha256}.json"
                    target.write_bytes(target.read_bytes() + b"tampered")
                    input_value = confirmed
                with open_pipeline_run(
                    {"项目名称": "匿名明星活动"}, root / "assets", root / "source",
                    cache_fingerprint=f"formal-{case}",
                ) as state:
                    self._complete_prerequisites(state)
                    with self.assertRaises(RuntimeError):
                        run_script_plan_stage(
                            state, briefs=[], constraints={}, source_view="ai-spec",
                            builder=lambda rows: _generate(rows, root, {}, None, "ai-spec"),
                            confirmed_brief_input=input_value,
                            confirmed_brief_resolver=service.resolver(),
                        )
                stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
            self.assertEqual(stages["script_plan"]["status"], "blocked")
            self.assertEqual(stages["script_draft"]["attempt"], 0)

    def test_production_v2_rejects_non_ai_spec_source_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                self.brief,
                root / "assets",
                root / "source",
                cache_fingerprint="planning-fixture",
            ) as state:
                self._complete_prerequisites(state)
                with patch(
                    "mvp_v2.loop_runner.resolve_script_type_workbook",
                    return_value=root / "anonymous-library.xlsx",
                ), patch(
                    "mvp_v2.loop_runner.load_template_slots",
                    return_value=self.slots,
                ), patch(
                    "mvp_v2.loop_runner.load_creative_variants",
                    return_value=[],
                ):
                    with self.assertRaisesRegex(RuntimeError, "legacy_human_source_view"):
                        run_script_plan_stage(
                            state,
                            briefs=[self.brief],
                            constraints={},
                            source_view="human",
                            builder=lambda: _generate(
                                [self.brief], root, {}, None, "human"
                            ),
                        )

            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["stages"]["script_plan"]["status"], "blocked")
        self.assertEqual(
            manifest["stages"]["script_plan"]["error"]["code"],
            "planning_audit_blocked",
        )
        self.assertEqual(manifest["stages"]["script_draft"]["attempt"], 0)

    def test_overloaded_v2_plan_is_saved_with_warning(self):
        constraints = {"planned_shot_count": 3}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                self.brief,
                root / "assets",
                root / "source",
                cache_fingerprint="planning-fixture",
            ) as state:
                self._complete_prerequisites(state)
                with patch(
                    "mvp_v2.loop_runner.resolve_script_type_workbook",
                    return_value=root / "anonymous-library.xlsx",
                ), patch(
                    "mvp_v2.loop_runner.load_ai_spec_shots",
                    return_value=(self.slots, []),
                ):
                    plan = run_script_plan_stage(
                        state,
                        briefs=[self.brief],
                        constraints=constraints,
                        source_view="ai-spec",
                        builder=lambda: _generate(
                            [self.brief], root, constraints, None, "ai-spec"
                        ),
                    )

            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(plan.planning_audit["status"], "needs_clarification")
        self.assertIn("capacity_requires_choice", plan.planning_audit["clarification_codes"])
        self.assertEqual(manifest["stages"]["script_plan"]["status"], "succeeded")
        self.assertEqual(manifest["stages"]["script_draft"]["attempt"], 0)

    def test_pending_fact_v2_plan_is_saved_for_clarification_before_draft(self):
        brief = dict(self.brief)
        brief["planning_events"] = [
            {"title": "待确认艺人致辞", "evidence": "是否致辞待确认"},
            {"title": "品牌落版", "evidence": "Brief明确要求品牌落版"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                brief,
                root / "assets",
                root / "source",
                cache_fingerprint="planning-fixture",
            ) as state:
                self._complete_prerequisites(state)
                with patch(
                    "mvp_v2.loop_runner.resolve_script_type_workbook",
                    return_value=root / "anonymous-library.xlsx",
                ), patch(
                    "mvp_v2.loop_runner.load_ai_spec_shots",
                    return_value=(self.slots, []),
                ):
                    plan = run_script_plan_stage(
                        state,
                        briefs=[brief],
                        constraints={},
                        source_view="ai-spec",
                        builder=lambda: _generate(
                            [brief], root, {}, None, "ai-spec"
                        ),
                    )

            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(plan.planning_audit["status"], "needs_clarification")
        self.assertEqual(manifest["stages"]["script_plan"]["status"], "succeeded")
        self.assertEqual(manifest["stages"]["script_draft"]["attempt"], 0)

    @staticmethod
    def _complete_prerequisites(state):
        refs = {}
        for stage_id in (
            "input_freeze",
            "source_catalog",
            "media_manifest",
            "visual_analysis",
        ):
            path = state.run_dir / f"{stage_id}.fixture"
            path.write_text(stage_id, encoding="utf-8")
            start_pipeline_stage(
                state,
                stage_id,
                input_sha256=f"input-{stage_id}",
                input_refs=tuple(refs.values()),
            )
            finish_pipeline_stage(
                state,
                stage_id,
                StageStatus.SUCCEEDED,
                output_sha256=sha256_file(path),
                output_refs=(str(path.resolve()),),
            )
            refs[stage_id] = str(path.resolve())


if __name__ == "__main__":
    unittest.main()
