import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from mvp_v2.ai_spec import refresh_ai_spec
from mvp_v2.audit import add_audit_checks
from mvp_v2.excel_io import (
    ADJACENT_REVIEW_SHEET_NAME,
    read_managed_sheet_rows,
    read_text_brief_file,
    write_template_output_workbook,
)
from mvp_v2.generation_stages import (
    _confirmed_brief_evidence, _planning_brief_from_confirmed_payload,
    _complete_local_draft_business_content,
    _validate_draft_business_content,
    _validate_no_duplicate_shot_content,
    _validate_no_unconfirmed_template_mechanisms,
    _preserve_unaffected_parent_script_text,
    run_machine_audit_stage,
    run_script_draft_stage,
    run_script_plan_stage,
    run_visual_binding_stage,
    run_workbook_render_stage,
)
from mvp_v2.models import SourceShot, TemplateDecision
from mvp_v2.pipeline_runtime import open_pipeline_run
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
from mvp_v2.run_contract import sha256_file
from mvp_v2.script_draft import ScriptDraftProviderError
from mvp_v2.stage_contract import StageStatus
from mvp_v2.stage_runtime import finish_pipeline_stage, start_pipeline_stage
from mvp_v2.loop_runner import run_generation_loop
from tests.test_mvp_v2 import (
    FIXED_REAL_BRIEF_TEXT,
    full_launch_template_rows,
    full_launch_vision_analysis,
    write_full_launch_media,
    write_launch_library,
)


class _Authority:
    def __init__(self, confirmed, raw):
        self.confirmed, self.raw = confirmed, raw

    def load_confirmed(self, value):
        return self.confirmed

    def read_payload_bytes(self, _payload):
        return self.raw


class _ResolutionAuthority(_Authority):
    def __init__(self, confirmed, raw, evidence_raw):
        super().__init__(confirmed, raw)
        self.evidence_raw = evidence_raw

    def read_payload_bytes(self, payload):
        return self.raw if payload == self.confirmed.payload else self.evidence_raw


class _CreativeStandardAuthority:
    def __init__(self, confirmed, raw):
        self.confirmed, self.raw = confirmed, raw

    def load_confirmed_creative_standard(self, _value):
        return self.confirmed

    def read_creative_standard_bytes(self, _payload):
        return self.raw


class GenerationStageTests(unittest.TestCase):
    def test_selected_creative_expression_cannot_authorize_new_unconfirmed_events(self):
        from types import SimpleNamespace
        from mvp_v2.script_draft import ScriptDraftError

        expression = "未来设想：嘉宾致辞，实际安排待确认"
        plan = SimpleNamespace(brief={"必须出现的内容": "客户交流"}, shot_plans=[
            {"shot_no": "1", "generated_structure_only": False,
             "creative_block": {"fields": {"画面描述": expression, "声音": "环境声音"}},
             "events": [{"event_id": "creative_block_1", "text": expression}],
             "excluded_source_mechanisms": []},
            {"shot_no": "2", "structure": "核心宣讲与仪式", "generated_structure_only": True,
             "events": [{"event_id": "template_slot_002", "text": "核心节奏"}],
             "excluded_source_mechanisms": []},
        ])
        rows = [{"镜号": "1", "画面描述": expression, "声音": "环境声音"},
                {"镜号": "2", "画面描述": "场内光影逐渐收拢", "声音": "环境声音"}]
        _validate_no_unconfirmed_template_mechanisms({"script_rows": rows}, plan)
        with self.assertRaisesRegex(ScriptDraftError, "技术结构标签虚构"):
            _validate_no_unconfirmed_template_mechanisms({"script_rows": [rows[0],
                {**rows[1], "画面描述": "嘉宾在台上正式致辞"}]}, plan)

    def test_business_validation_keeps_structure_checks_without_keyword_gate(self):
        from types import SimpleNamespace
        from mvp_v2.script_draft import ScriptDraftError

        rows = [{"镜号": str(n), "画面描述": visual, "声音": "现场环境声", "时长(秒)": 4}
                for n, visual in [(1, "镜头推进，捕捉观众挥手回应。"), (2, "工作人员向粉丝派发花茶。")]]
        plan = SimpleNamespace(
            script_rows=rows,
            detail_rows=[{"采用来源文件": "匿名库.xlsx", "采用来源 sheet": "模版", "采用来源行": n}
                         for n in (7, 8)],
            shot_plans=[
                {"generated_structure_only": True, "events": [{"event_id": "template_slot_001", "text": "核心节奏·中景承接", "role": "primary"}]},
                {"generated_structure_only": False, "events": [{"event_id": "brief_event_001", "text": "派发花茶", "role": "primary"}]},
            ],
        )
        before = json.loads(json.dumps(rows))
        _complete_local_draft_business_content({"script_rows": rows}, plan)
        self.assertEqual(rows, before)
        _validate_draft_business_content({"script_rows": rows}, plan)
        wrong = [rows[0], {**rows[1], "画面描述": "灯光扫过会场。"}]
        _validate_draft_business_content({"script_rows": wrong}, plan)
        with self.assertRaises(ScriptDraftError):
            _validate_draft_business_content({"script_rows": [rows[0], {**rows[1], "画面描述": ""}]}, plan)

    def test_draft_rejects_unconfirmed_template_fact_and_exact_duplicate_shots(self):
        class Plan:
            shot_plans = [{"excluded_source_mechanisms": ["艺人", "启动仪式"]}]

        with self.assertRaisesRegex(Exception, "Brief 未确认的模板内容"):
            _validate_no_unconfirmed_template_mechanisms({
                "script_rows": [{
                    "镜号": "1", "画面描述": "艺人持麦亮相", "声音": "现场音",
                }],
            }, Plan())

        with self.assertRaisesRegex(Exception, "画面和声音完全重复"):
            _validate_no_duplicate_shot_content({
                "script_rows": [
                    {"镜号": "1", "画面描述": "情侣拉开隔板", "声音": "现场欢呼"},
                    {"镜号": "2", "画面描述": " 情侣拉开隔板 ", "声音": "现场欢呼"},
                ],
            })

        class PlaceholderPlan:
            shot_plans = [
                {
                    "shot_no": "1", "structure": "艺人亮相",
                    "generated_structure_only": True, "events": [],
                    "excluded_source_mechanisms": [],
                },
                {
                    "shot_no": "2", "structure": "核心仪式",
                    "generated_structure_only": True, "events": [],
                    "excluded_source_mechanisms": [],
                },
            ]

        with self.assertRaisesRegex(Exception, "技术结构标签虚构"):
            _validate_no_unconfirmed_template_mechanisms({
                "script_rows": [
                    {"镜号": "1", "画面描述": "艺人挥手亮相", "声音": "现场声"},
                    {"镜号": "2", "画面描述": "环境光影推进", "声音": "环境声"},
                ],
            }, PlaceholderPlan())
    def test_formal_projection_blocks_pending_unknown_or_missing_required_facts(self):
        cases = (
            ("pending", {"项目名称": "匿名发布会", "脚本类型": "发布会", "视频时长": "4s"}, "pending"),
            ("unknown", {"项目名称": "匿名发布会", "脚本类型": "发布会", "视频时长": "4s", "自由推断": "禁止"}, "confirmed"),
            ("missing", {"项目名称": "匿名发布会", "脚本类型": "发布会"}, "confirmed"),
        )
        for name, brief, certainty in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                confirmed, raw = self._formal_confirmed(brief, certainty=certainty)
                with open_pipeline_run(brief, root / "assets", root / "source", cache_fingerprint=f"projection-{name}") as state:
                    self._complete_prerequisites(state)
                    with self.assertRaises(RuntimeError):
                        run_script_plan_stage(state, briefs=[], constraints={}, source_view="human", builder=lambda rows: self._business_plan(brief=rows[0]), confirmed_brief_input=confirmed, confirmed_brief_resolver=ConfirmedBriefResolver(_Authority(confirmed, raw)))
                stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
            self.assertEqual(stages["script_plan"]["error"]["code"], "confirmed_brief_needs_clarification")
            self.assertEqual(stages["script_draft"]["attempt"], 0)

    def test_project_name_context_preserves_explicit_name_and_rejects_foreign_project(self):
        confirmed, raw = self._formal_confirmed({"脚本类型": "发布会", "视频时长": "60秒"})
        payload = json.loads(raw)["brief"]
        context = {"authority": "projects.name", "workspace_id": confirmed.workspace_id,
                   "project_id": confirmed.project_id, "project_version": 2, "name": "匿名项目名称"}
        brief, _ = _planning_brief_from_confirmed_payload(payload, _confirmed_brief_evidence(confirmed), project_context=context)
        self.assertEqual(brief["项目名称"], "匿名项目名称")
        self.assertNotIn("项目名称", brief["_formal_fact_evidence"])
        with self.assertRaisesRegex(ValueError, "项目名称来源与当前项目不一致"):
            _planning_brief_from_confirmed_payload(payload, _confirmed_brief_evidence(confirmed),
                project_context={**context, "project_id": "another-project"})
        named, raw_named = self._formal_confirmed({"项目名称": "明确的片名", "脚本类型": "发布会", "视频时长": "60秒"})
        brief, _ = _planning_brief_from_confirmed_payload(json.loads(raw_named)["brief"], _confirmed_brief_evidence(named), project_context=context)
        self.assertEqual(brief["项目名称"], "明确的片名")
        self.assertNotIn("_formal_project_context", brief)

    def test_formal_t01_resolver_blocks_authority_drift_sha_and_identity_mismatch(self):
        brief = {"项目名称": "匿名发布会"}
        confirmed, raw = self._formal_confirmed(brief)
        cases = (
            ("workspace", ConfirmedBriefResolver(_Authority(
                ConfirmedBriefInput(**{**confirmed.__dict__, "workspace_id": "other-workspace"}), raw)), "confirmed_brief_resolution_failed"),
            ("project", ConfirmedBriefResolver(_Authority(
                ConfirmedBriefInput(**{**confirmed.__dict__, "project_id": "other-project"}), raw)), "confirmed_brief_resolution_failed"),
            ("brief_version", ConfirmedBriefResolver(_Authority(
                ConfirmedBriefInput(**{**confirmed.__dict__, "brief_version_id": "brief-v2"}), raw)), "confirmed_brief_resolution_failed"),
            ("sha", ConfirmedBriefResolver(_Authority(confirmed, raw + b"x")), "confirmed_brief_resolution_failed"),
        )
        for name, resolver, error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with open_pipeline_run(brief, root / "assets", root / "source", cache_fingerprint=name) as state:
                    self._complete_prerequisites(state)
                    with self.assertRaises(RuntimeError):
                        run_script_plan_stage(state, briefs=[], constraints={}, source_view="human", builder=lambda rows: self._business_plan(brief=rows[0]), confirmed_brief_input=confirmed, confirmed_brief_resolver=resolver)
                stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
                self.assertEqual(stages["script_plan"]["error"]["code"], error)
                self.assertEqual(stages["script_draft"]["attempt"], 0)
    def test_v10_alpha_api_exposes_no_raw_briefs_or_draft_generation_entry(self):
        source = (
            Path(__file__).parents[1] / "mvp_v2" / "alpha_console.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("run_generation_loop", source)
        self.assertNotIn("run_generation_once", source)
        self.assertNotIn('quality_profile="draft"', source)
        self.assertNotIn('"briefs"', source)

    def test_confirmed_brief_blocks_missing_unconfirmed_conflicted_or_drifting_inputs(self):
        confirmed, raw = self._formal_confirmed({"项目名称": "匿名发布会"})
        cases = (
            ("resolver_missing", confirmed, None, "confirmed_brief_dependency_missing"),
            (
                "unconfirmed",
                ConfirmedBriefInput(**{**confirmed.__dict__, "confirmation_status": "pending"}),
                ConfirmedBriefResolver(_Authority(confirmed, raw)),
                "confirmed_brief_not_confirmed",
            ),
            (
                "conflicted",
                ConfirmedBriefInput(**{**confirmed.__dict__, "unresolved_conflict_count": 1}),
                ConfirmedBriefResolver(_Authority(confirmed, raw)),
                "confirmed_brief_conflicts_unresolved",
            ),
        )
        for name, confirmed_input, resolver, error_code in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with open_pipeline_run(
                    {"项目名称": "匿名发布会"},
                    root / "assets",
                    root / "source",
                    cache_fingerprint=f"confirmed-{name}",
                ) as state:
                    self._complete_prerequisites(state)
                    with self.assertRaises(RuntimeError):
                        run_script_plan_stage(
                            state,
                            briefs=[],
                            constraints={},
                            source_view="human",
                            builder=lambda _briefs: self._business_plan(),
                            confirmed_brief_input=confirmed_input,
                            confirmed_brief_resolver=resolver,
                        )
                stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
                self.assertEqual(stages["script_plan"]["status"], "blocked")
                self.assertEqual(stages["script_plan"]["error"]["code"], error_code)
                self.assertEqual(stages["script_draft"]["attempt"], 0)

    def test_formal_conflict_payload_never_first_wins_or_trusts_cached_count(self):
        evidence_a = [{"artifact_ref": "artifact://evidence/a"}]
        evidence_b = [{"artifact_ref": "artifact://evidence/b"}]
        required = [
            {"fact_id": "fact_project_a", "field_path": "项目名称", "value_json": '"匿名甲"', "certainty": "confirmed", "evidence_refs": evidence_a},
            {"fact_id": "fact_project_b", "field_path": "项目名称", "value_json": '"匿名乙"', "certainty": "confirmed", "evidence_refs": evidence_b},
            {"fact_id": "fact_type", "field_path": "脚本类型", "value_json": '"发布会"', "certainty": "confirmed", "evidence_refs": evidence_a},
            {"fact_id": "fact_duration", "field_path": "视频时长", "value_json": '"60s"', "certainty": "confirmed", "evidence_refs": evidence_a},
        ]
        resolution = {
            "schema_version": "mplus-formal-resolution-evidence/v1",
            "workspace_id": "workspace-anonymous", "project_id": "project-anonymous",
            "brief_version_id": "brief-parent", "conflict_id": "conflict-parent",
            "selected_fact_id": "fact_project_a", "conflict_evidence_refs": evidence_a + evidence_b,
            "selected_fact_evidence_refs": evidence_a,
            "resolved_by_actor_id": "actor-anonymous", "resolved_at": "2026-08-14T00:00:00Z",
        }
        resolution_raw = json.dumps(resolution, ensure_ascii=False, sort_keys=True).encode()
        resolution_payload = {
            "ref": "artifact://formal-brief/resolution",
            "sha256": sha256(resolution_raw).hexdigest(),
        }
        cases = {
            "unresolved_cached_zero": [{
                "conflict_id": "conflict-current", "field_path": "项目名称",
                "fact_ids": ["fact_project_a", "fact_project_b"],
                "evidence_refs": evidence_a + evidence_b, "resolution_status": "unresolved",
            }],
            "wrong_selected_fact_evidence": [{
                "conflict_id": "conflict-current", "field_path": "项目名称",
                "fact_ids": ["fact_project_a", "fact_project_b"],
                "evidence_refs": evidence_a + evidence_b, "resolution_status": "resolved",
                "resolved_fact_id": "fact_project_b", "resolved_by_actor_id": "actor-anonymous",
                "resolved_at": "2026-08-14T00:00:00Z", "resolution_evidence": resolution_payload,
            }],
            "stale_resolved_fact_id": [{
                "conflict_id": "conflict-current", "field_path": "项目名称",
                "fact_ids": ["fact_project_a", "fact_project_b"],
                "evidence_refs": evidence_a + evidence_b, "resolution_status": "resolved",
                "resolved_fact_id": "fact-from-parent-version",
                "resolved_by_actor_id": "actor-anonymous",
                "resolved_at": "2026-08-14T00:00:00Z", "resolution_evidence": resolution_payload,
            }],
            "same_field_without_conflict": [],
        }
        for name, conflicts in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                brief = {"facts": required, "conflicts": conflicts}
                envelope = {
                    "schema_version": FORMAL_BRIEF_SCHEMA,
                    "workspace_id": "workspace-anonymous", "project_id": "project-anonymous",
                    "brief_version_id": "brief-v1", "brief": brief,
                }
                raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode()
                confirmed = ConfirmedBriefInput(
                    workspace_id="workspace-anonymous", project_id="project-anonymous",
                    brief_version_id="brief-v1", confirmation_id="confirmation-v1",
                    brief_snapshot_id="snapshot-v1",
                    payload=ArtifactPayload("artifact://brief/conflict.json", sha256(raw).hexdigest()),
                    confirmation_status="confirmed", unresolved_conflict_count=0,
                    confirmed_by_actor_id="actor-anonymous", confirmed_at="2026-08-14T00:00:01Z",
                )
                root = Path(tmp)
                builder_calls = []
                with open_pipeline_run(
                    {"项目名称": "匿名"}, root / "assets", root / "source",
                    cache_fingerprint=name,
                ) as state:
                    self._complete_prerequisites(state)
                    with self.assertRaises(RuntimeError):
                        run_script_plan_stage(
                            state, briefs=[], constraints={}, source_view="human",
                            builder=lambda _briefs: builder_calls.append("called"),
                            confirmed_brief_input=confirmed,
                            confirmed_brief_resolver=ConfirmedBriefResolver(
                                _ResolutionAuthority(confirmed, raw, resolution_raw)
                            ),
                        )
                stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
                self.assertEqual(builder_calls, [])
                self.assertEqual(stages["script_plan"]["status"], "blocked")
                self.assertEqual(stages["script_draft"]["attempt"], 0)

    def test_confirmed_brief_rejects_even_complete_dict_before_builder_or_resolver(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名发布会"},
                root / "assets",
                root / "source",
                cache_fingerprint="confirmed-dict-rejected",
            ) as state:
                self._complete_prerequisites(state)
                with self.assertRaises(RuntimeError):
                    run_script_plan_stage(
                        state,
                        briefs=[],
                        constraints={},
                        source_view="human",
                        builder=lambda _briefs: calls.append("builder"),
                        confirmed_brief_input=self._confirmed_brief_dict(),
                        confirmed_brief_resolver=ConfirmedBriefResolver(
                            _Authority(*self._formal_confirmed({"项目名称": "匿名发布会"}))
                        ),
                    )
            stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
        self.assertEqual(calls, [])
        self.assertEqual(stages["script_plan"]["error"]["code"], "confirmed_brief_input_invalid")
        self.assertEqual(stages["script_draft"]["attempt"], 0)

    def test_confirmed_brief_resolver_is_the_only_initial_planning_input(self):
        structured_brief = {"项目名称": "匿名发布会", "脚本类型": "发布会", "视频时长": "4s"}
        confirmed, raw = self._formal_confirmed(structured_brief)
        standard, standard_resolver = self._confirmed_creative_standard(confirmed)
        received = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名发布会"},
                root / "assets",
                root / "source",
                cache_fingerprint="confirmed-accepted",
            ) as state:
                self._complete_prerequisites(state)

                def build(planning_briefs):
                    received.append(planning_briefs)
                    return self._business_plan(brief=planning_briefs[0])

                artifact = run_script_plan_stage(
                    state,
                    briefs=[],
                    constraints={},
                    source_view="human",
                    builder=build,
                    confirmed_brief_input=confirmed,
                    confirmed_brief_resolver=ConfirmedBriefResolver(_Authority(confirmed, raw)),
                    confirmed_creative_standard_input=standard,
                    confirmed_creative_standard_resolver=standard_resolver,
                )
            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(received[0][0]["项目名称"], structured_brief["项目名称"])
        self.assertEqual(received[0][0]["脚本类型"], structured_brief["脚本类型"])
        self.assertEqual(artifact.briefs[0]["视频时长"], structured_brief["视频时长"])
        self.assertEqual(artifact.confirmed_brief_input["project_id"], confirmed.project_id)
        self.assertIn(confirmed.payload.ref, manifest["stages"]["script_plan"]["input_refs"])
        self.assertEqual(manifest["stages"]["script_plan"]["status"], "succeeded")

    def test_specific_activity_type_routes_generic_short_video_to_star_activity(self):
        structured_brief = {
            "项目名称": "匿名应援活动", "脚本类型": "活动短视频",
            "行业或活动类别": "明星活动类", "视频时长": "4s",
            "特殊需求": "使用明星歌曲作为背景音乐",
        }
        confirmed, raw = self._formal_confirmed(structured_brief)
        standard, standard_resolver = self._confirmed_creative_standard(confirmed)
        received = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名应援活动"}, root / "assets", root / "source",
                cache_fingerprint="specific-star-activity",
            ) as state:
                self._complete_prerequisites(state)

                def build(planning_briefs):
                    received.append(planning_briefs)
                    return self._business_plan(brief=planning_briefs[0])

                artifact = run_script_plan_stage(
                    state, briefs=[], constraints={}, source_view="human", builder=build,
                    confirmed_brief_input=confirmed,
                    confirmed_brief_resolver=ConfirmedBriefResolver(_Authority(confirmed, raw)),
                    confirmed_creative_standard_input=standard,
                    confirmed_creative_standard_resolver=standard_resolver,
                )
                payload = json.loads(artifact.path.read_text(encoding="utf-8"))

        self.assertEqual(received[0][0]["脚本类型"], "明星活动")
        self.assertEqual(received[0][0]["特殊需求"], "使用明星歌曲作为背景音乐")
        self.assertEqual(artifact.briefs[0]["脚本类型"], "明星活动")
        self.assertIn("活动短视频", raw.decode())
        resolutions = [
            item["script_type_resolution"]
            for item in payload["confirmed_brief_provenance"]["facts"]
            if "script_type_resolution" in item
        ]
        self.assertTrue(resolutions)
        self.assertEqual(resolutions[0]["authority"], "industry_or_activity_category")

    def test_descriptive_specific_activity_type_routes_to_launch(self):
        structured_brief = {
            "项目名称": "匿名全球大会", "脚本类型": "活动短视频",
            "行业或活动类别": "发布会活动回顾视频", "视频时长": "60s",
        }
        confirmed, raw = self._formal_confirmed(structured_brief)
        standard, standard_resolver = self._confirmed_creative_standard(confirmed)
        received = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名全球大会"}, root / "assets", root / "source",
                cache_fingerprint="descriptive-launch-activity",
            ) as state:
                self._complete_prerequisites(state)
                run_script_plan_stage(
                    state, briefs=[], constraints={}, source_view="human",
                    builder=lambda rows: received.extend(rows) or self._business_plan(brief=rows[0]),
                    confirmed_brief_input=confirmed,
                    confirmed_brief_resolver=ConfirmedBriefResolver(_Authority(confirmed, raw)),
                    confirmed_creative_standard_input=standard,
                    confirmed_creative_standard_resolver=standard_resolver,
                )

        self.assertEqual(received[0]["脚本类型"], "发布会")

    def test_conflicting_supported_script_and_specific_types_require_clarification(self):
        structured_brief = {
            "项目名称": "匿名活动", "脚本类型": "发布会",
            "行业或活动类别": "明星活动类", "视频时长": "4s",
        }
        confirmed, raw = self._formal_confirmed(structured_brief)
        standard, standard_resolver = self._confirmed_creative_standard(confirmed)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名活动"}, root / "assets", root / "source",
                cache_fingerprint="conflicting-activity-types",
            ) as state:
                self._complete_prerequisites(state)
                with self.assertRaises(RuntimeError):
                    run_script_plan_stage(
                        state, briefs=[], constraints={}, source_view="human",
                        builder=lambda rows: self._business_plan(brief=rows[0]),
                        confirmed_brief_input=confirmed,
                        confirmed_brief_resolver=ConfirmedBriefResolver(_Authority(confirmed, raw)),
                        confirmed_creative_standard_input=standard,
                        confirmed_creative_standard_resolver=standard_resolver,
                    )
            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["stages"]["script_plan"]["error"]["code"],
            "confirmed_brief_needs_clarification",
        )

    def test_confirmed_brief_requires_provider_and_blocks_when_unavailable(self):
        confirmed, raw = self._formal_confirmed({"项目名称": "匿名发布会", "脚本类型": "发布会", "视频时长": "4s"})
        standard, standard_resolver = self._confirmed_creative_standard(confirmed)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名发布会"},
                root / "assets",
                root / "source",
                cache_fingerprint="confirmed-provider",
            ) as state:
                self._complete_prerequisites(state)
                plan = run_script_plan_stage(
                    state,
                    briefs=[],
                    constraints={},
                    source_view="human",
                    builder=lambda planning_briefs: self._business_plan(
                        brief=planning_briefs[0]
                    ),
                    confirmed_brief_input=confirmed,
                    confirmed_brief_resolver=ConfirmedBriefResolver(_Authority(confirmed, raw)),
                    confirmed_creative_standard_input=standard,
                    confirmed_creative_standard_resolver=standard_resolver,
                )
                with self.assertRaises(RuntimeError):
                    run_script_draft_stage(
                        state,
                        script_plan_path=plan.path,
                        quality_profile="draft",
                    )
                with patch(
                    "mvp_v2.generation_stages.create_script_draft",
                    side_effect=ScriptDraftProviderError("credentials unavailable"),
                ), self.assertRaises(RuntimeError):
                    run_script_draft_stage(
                        state,
                        script_plan_path=plan.path,
                        quality_profile="client-review",
                    )
            stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]

        self.assertEqual(stages["script_draft"]["status"], "blocked")
        self.assertEqual(stages["script_draft"]["attempt"], 2)
        self.assertEqual(
            stages["script_draft"]["error"]["code"],
            "confirmed_brief_provider_unavailable",
        )
        self.assertEqual(stages["visual_binding"]["attempt"], 0)

    def test_draft_stage_rejects_invalid_duration_before_visual_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名发布会"},
                root / "assets",
                root / "source",
                cache_fingerprint="draft-content",
            ) as state:
                self._complete_prerequisites(state)
                plan = run_script_plan_stage(
                    state,
                    briefs=[{"项目名称": "匿名发布会"}],
                    constraints={},
                    source_view="human",
                    builder=lambda: self._business_plan(duration=""),
                )
                with self.assertRaisesRegex(RuntimeError, "时长必须是正整数秒"):
                    run_script_draft_stage(
                        state,
                        script_plan_path=plan.path,
                        quality_profile="draft",
                    )

            stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
            self.assertEqual(stages["script_draft"]["status"], "blocked")
            self.assertEqual(stages["script_draft"]["attempt"], 1)
            self.assertEqual(stages["visual_binding"]["attempt"], 0)

    def test_draft_stage_keeps_nonempty_business_content_and_source_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名发布会"},
                root / "assets",
                root / "source",
                cache_fingerprint="draft-content",
            ) as state:
                self._complete_prerequisites(state)
                plan = run_script_plan_stage(
                    state,
                    briefs=[{"项目名称": "匿名发布会"}],
                    constraints={},
                    source_view="human",
                    builder=lambda: self._business_plan(visual_desc="", sound=""),
                )
                draft = run_script_draft_stage(
                    state,
                    script_plan_path=plan.path,
                    quality_profile="draft",
                )

            self.assertEqual(draft.path.name, "script_draft.json")
            self.assertEqual(draft.script_rows[0]["画面描述"], "待完善画面：开场")
            self.assertEqual(draft.script_rows[0]["声音"], "待确认声音：开场")
            self.assertEqual(draft.script_rows[0]["时长(秒)"], "4")
            self.assertEqual(draft.detail_rows[0]["采用来源文件"], "匿名模板.xlsx")
            self.assertEqual(draft.detail_rows[0]["采用来源 sheet"], "模版")
            self.assertEqual(draft.detail_rows[0]["采用来源行"], 7)
    def test_targeted_text_revision_preserves_every_unrequested_parent_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "script_draft.json"
            parent.write_text(json.dumps({
                "overall_summary": "父版本说明",
                "script_rows": [
                    {"镜号": "1", "画面描述": "父镜1", "声音": "父声1"},
                    {"镜号": "2", "画面描述": "父镜2", "声音": "父声2"},
                ],
                "pending_confirmations": [
                    {"shot_no": "1", "item": "父待确认"},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            result = _preserve_unaffected_parent_script_text(
                {
                    "overall_summary": "模型新说明",
                    "script_rows": [
                        {"镜号": "1", "画面描述": "模型改坏镜1", "声音": "模型声1"},
                        {"镜号": "2", "画面描述": "只改镜2", "声音": "新声2"},
                    ],
                    "pending_confirmations": [
                        {"shot_no": "2", "item": "新待确认"},
                    ],
                    "usage": {},
                },
                {"user_feedback": [{
                    "category": "detail",
                    "scope": "shot",
                    "shot_no": "2",
                }]},
                parent,
            )

        self.assertEqual(result["overall_summary"], "父版本说明")
        self.assertEqual(result["script_rows"][0]["画面描述"], "父镜1")
        self.assertEqual(result["script_rows"][1]["画面描述"], "只改镜2")
        self.assertEqual(result["pending_confirmations"][0]["item"], "父待确认")
        self.assertEqual(result["pending_confirmations"][1]["item"], "新待确认")
        self.assertEqual(result["usage"]["targeted_revision_shots"], ["2"])
        self.assertEqual(result["usage"]["preserved_parent_text_shot_count"], 1)

    def test_five_generation_stages_persist_sha_refs_and_reload_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名发布会"},
                root / "assets",
                root / "source",
                cache_fingerprint="visual-fingerprint",
            ) as state:
                self._complete_prerequisites(state)
                build_calls = []
                bind_calls = []
                render_calls = []
                audit_calls = []

                def build_plan():
                    build_calls.append(True)
                    slots = [self._slot(index) for index in range(1, 18)]
                    decisions = [
                        TemplateDecision(
                            slot=slot,
                            variant=None,
                            mode="KEEP",
                            score=0,
                            match_reason="保留模板",
                        )
                        for slot in slots
                    ]
                    script_rows = [
                        {
                            "镜号": str(index),
                            "结构": f"结构{index}",
                            "时长(秒)": "4",
                        }
                        for index in range(1, 18)
                    ]
                    detail_rows = [
                        {
                            "slot_index": index,
                            "模板镜号": str(index),
                            "采用来源文件": "匿名模板.xlsx",
                            "采用来源 sheet": "模版",
                            "采用来源行": index + 6,
                        }
                        for index in range(1, 18)
                    ]
                    return (
                        script_rows,
                        detail_rows,
                        decisions,
                        slots,
                        {"项目名称": "匿名发布会"},
                    )

                plan = run_script_plan_stage(
                    state,
                    briefs=[{"项目名称": "匿名发布会"}],
                    constraints={},
                    source_view="human",
                    builder=build_plan,
                )
                self.assertEqual(len(plan.script_rows), 17)
                self.assertEqual(len(plan.decisions), 17)
                self.assertEqual(
                    plan.source_refs[0],
                    {
                        "source_file": "匿名模板.xlsx",
                        "source_sheet": "模版",
                        "source_row": 7,
                    },
                )

                def bind_visuals(reloaded):
                    bind_calls.append(reloaded.path)
                    details = [dict(row) for row in reloaded.detail_rows]
                    visual_rows = []
                    for index, detail in enumerate(details, start=1):
                        detail["image_id"] = f"img-{index}"
                        detail["配图判断"] = "匹配"
                        detail["图片绑定说明"] = f"依据-{index}"
                        visual_rows.append({
                            "slot_index": index,
                            "image_id": f"img-{index}",
                            "原图路径": f"/tmp/img-{index}.png",
                            "配图判断": "匹配",
                            "绑定说明": f"依据-{index}",
                        })
                    return reloaded.script_rows, details, visual_rows

                binding = run_visual_binding_stage(
                    state,
                    script_plan_path=plan.path,
                    binder=bind_visuals,
                )
                self.assertEqual(bind_calls, [state.run_dir / "script_draft.json"])
                self.assertEqual(len(binding.bindings), 17)
                self.assertEqual(binding.bindings[0]["basis"], "依据-1")

                def render_workbook(reloaded, target, audit_rows):
                    render_calls.append((reloaded.path, list(audit_rows)))
                    workbook = Workbook()
                    workbook.active.title = "脚本"
                    workbook.create_sheet(ADJACENT_REVIEW_SHEET_NAME)
                    workbook.save(target)
                    workbook.close()

                rendered = run_workbook_render_stage(
                    state,
                    visual_binding_path=binding.path,
                    renderer=render_workbook,
                )
                workbook = load_workbook(rendered.path, read_only=True)
                workbook.close()

                def audit_workbook(reloaded, workbook_path):
                    audit_calls.append((reloaded.path, workbook_path))
                    return {
                        "status": "pass",
                        "hard_fail_count": 0,
                        "warning_count": 0,
                        "score": 100,
                        "blocking_items": [],
                        "next_constraints": {},
                        "checks": [],
                    }

                final_workbook = state.run_dir / "result.loop1.xlsx"
                final_audit = state.run_dir / "result.loop1.audit.json"
                result = run_machine_audit_stage(
                    state,
                    visual_binding_path=binding.path,
                    workbook_render_path=rendered.path,
                    output_path=final_workbook,
                    audit_path=final_audit,
                    auditor=audit_workbook,
                    renderer=render_workbook,
                )

                self.assertEqual(build_calls, [True])
                self.assertEqual(audit_calls, [(binding.path, rendered.path)])
                self.assertTrue(result.output_path.is_file())
                self.assertTrue(result.audit_path.is_file())
                self.assertEqual(
                    json.loads(result.audit_path.read_text(encoding="utf-8"))["status"],
                    "pass",
                )

            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
            for stage_id in (
                "script_plan",
                "script_draft",
                "visual_binding",
                "workbook_render",
                "machine_audit",
            ):
                stage = manifest["stages"][stage_id]
                self.assertEqual(stage["status"], "succeeded")
                self.assertEqual(stage["attempt"], 1)
                self.assertTrue(stage["input_sha256"])
                self.assertTrue(stage["output_sha256"])
                self.assertTrue(stage["input_refs"])
                self.assertTrue(stage["output_refs"])
                self.assertTrue(all(Path(ref).is_file() for ref in stage["output_refs"]))

    def test_script_plan_contract_block_stops_downstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名发布会"},
                root / "assets",
                root / "source",
                cache_fingerprint="visual-fingerprint",
            ) as state:
                self._complete_prerequisites(state)
                with self.assertRaisesRegex(RuntimeError, "未生成可用镜头计划"):
                    run_script_plan_stage(
                        state,
                        briefs=[{"项目名称": "匿名发布会"}],
                        constraints={},
                        source_view="human",
                        builder=lambda: ([], [], [], [], {"项目名称": "匿名发布会"}),
                    )

            stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
            self.assertEqual(stages["script_plan"]["status"], "blocked")
            self.assertEqual(stages["script_plan"]["error"]["category"], "contract")
            self.assertEqual(stages["visual_binding"]["attempt"], 0)
            self.assertEqual(stages["workbook_render"]["attempt"], 0)
            self.assertEqual(stages["machine_audit"]["attempt"], 0)

    def test_machine_audit_failure_is_blocked_not_execution_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名发布会"},
                root / "assets",
                root / "source",
                cache_fingerprint="visual-fingerprint",
            ) as state:
                self._complete_prerequisites(state)
                plan = run_script_plan_stage(
                    state,
                    briefs=[{"项目名称": "匿名发布会"}],
                    constraints={},
                    source_view="human",
                    builder=self._single_plan,
                )
                binding = run_visual_binding_stage(
                    state,
                    script_plan_path=plan.path,
                    binder=lambda item: (item.script_rows, item.detail_rows, []),
                )
                rendered = run_workbook_render_stage(
                    state,
                    visual_binding_path=binding.path,
                    renderer=self._simple_renderer,
                )
                result = run_machine_audit_stage(
                    state,
                    visual_binding_path=binding.path,
                    workbook_render_path=rendered.path,
                    output_path=state.run_dir / "failed.loop1.xlsx",
                    audit_path=state.run_dir / "failed.loop1.audit.json",
                    auditor=lambda item, path: {
                        "status": "fail",
                        "hard_fail_count": 1,
                        "warning_count": 0,
                        "score": 75,
                        "blocking_items": ["required_content_evidence_missing"],
                        "next_constraints": {"force_keep_slots": [1]},
                        "checks": [],
                    },
                    renderer=self._simple_renderer,
                )

            self.assertEqual(result.audit["status"], "fail")
            stage = json.loads(state.manifest_path.read_text(encoding="utf-8"))[
                "stages"
            ]["machine_audit"]
            self.assertEqual(stage["status"], "blocked")
            self.assertEqual(stage["error"]["category"], "contract")
            self.assertNotEqual(stage["error"]["category"], "execution")

    def test_workbook_write_failure_preserves_successful_plan_and_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名发布会"},
                root / "assets",
                root / "source",
                cache_fingerprint="visual-fingerprint",
            ) as state:
                self._complete_prerequisites(state)
                plan = run_script_plan_stage(
                    state,
                    briefs=[{"项目名称": "匿名发布会"}],
                    constraints={},
                    source_view="human",
                    builder=self._single_plan,
                )
                binding = run_visual_binding_stage(
                    state,
                    script_plan_path=plan.path,
                    binder=lambda item: (item.script_rows, item.detail_rows, []),
                )
                with self.assertRaisesRegex(RuntimeError, "disk unavailable"):
                    run_workbook_render_stage(
                        state,
                        visual_binding_path=binding.path,
                        renderer=lambda item, target, rows: (
                            _ for _ in ()
                        ).throw(OSError("disk unavailable")),
                    )

            stages = json.loads(state.manifest_path.read_text(encoding="utf-8"))["stages"]
            self.assertEqual(stages["script_plan"]["status"], "succeeded")
            self.assertEqual(stages["visual_binding"]["status"], "succeeded")
            self.assertTrue(Path(stages["script_plan"]["output_refs"][0]).is_file())
            self.assertTrue(Path(stages["visual_binding"]["output_refs"][0]).is_file())
            self.assertEqual(stages["workbook_render"]["status"], "failed")
            self.assertEqual(stages["workbook_render"]["error"]["category"], "system")
            self.assertEqual(stages["machine_audit"]["attempt"], 0)

    def test_visual_semantic_input_change_invalidates_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名发布会"},
                root / "assets",
                root / "source",
                cache_fingerprint="visual-fingerprint",
            ) as state:
                self._complete_prerequisites(state)
                plan = run_script_plan_stage(
                    state,
                    briefs=[{"项目名称": "匿名发布会"}],
                    constraints={},
                    source_view="human",
                    builder=self._single_plan,
                )
                calls = []

                def bind(item):
                    calls.append(True)
                    return item.script_rows, item.detail_rows, []

                first = run_visual_binding_stage(
                    state,
                    script_plan_path=plan.path,
                    binder=bind,
                    semantic_inputs={
                        "rematch_visuals": False,
                        "vision_analysis": {"img-1": {"caption": "A"}},
                        "image_manifest_rows": [{"image_id": "img-1"}],
                        "image_analysis_rows": [{"image_id": "img-1", "caption": "A"}],
                    },
                )
                first_hash = json.loads(
                    state.manifest_path.read_text(encoding="utf-8")
                )["stages"]["visual_binding"]["input_sha256"]
                second = run_visual_binding_stage(
                    state,
                    script_plan_path=plan.path,
                    binder=bind,
                    semantic_inputs={
                        "rematch_visuals": True,
                        "vision_analysis": {"img-1": {"caption": "B"}},
                        "image_manifest_rows": [{"image_id": "img-1"}],
                        "image_analysis_rows": [{"image_id": "img-1", "caption": "B"}],
                    },
                )

            self.assertEqual(first.path, second.path)
            self.assertEqual(calls, [True, True])
            stage = json.loads(state.manifest_path.read_text(encoding="utf-8"))[
                "stages"
            ]["visual_binding"]
            self.assertEqual(stage["attempt"], 2)
            self.assertNotEqual(first_hash, stage["input_sha256"])

    def test_generation_loop_runtime_passes_four_nodes_and_matches_legacy_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            source_root.mkdir()
            write_full_launch_media(source_root)
            workbook_path = source_root / "M+发布会类通用脚本库.xlsx"
            write_launch_library(
                workbook_path,
                full_launch_template_rows(),
                [],
            )
            refresh_ai_spec(workbook_path)
            brief_path = root / "anonymous-brief.txt"
            brief_path.write_text(FIXED_REAL_BRIEF_TEXT, encoding="utf-8")
            brief = read_text_brief_file(brief_path)
            analysis = full_launch_vision_analysis()
            legacy = run_generation_loop(
                [brief],
                source_root,
                root / "legacy.xlsx",
                max_loops=3,
                vision_analysis=analysis,
                source_view="ai-spec",
            )

            with open_pipeline_run(
                brief,
                root / "assets",
                source_root,
                cache_fingerprint="visual-fingerprint",
            ) as state:
                self._complete_prerequisites(state)
                staged = run_generation_loop(
                    [brief],
                    source_root,
                    state.run_dir / "staged.xlsx",
                    max_loops=3,
                    vision_analysis=analysis,
                    source_view="ai-spec",
                    runtime=state,
                )

            self.assertEqual(legacy.exit_code, 0)
            self.assertEqual(staged.exit_code, 0)
            self.assertEqual(staged.passed_iteration, legacy.passed_iteration)
            # Both paths retain the same technical audit. The staged writer
            # additionally realizes planned requirements in their assigned shots.
            plan = json.loads((state.run_dir / "script_plan.json").read_text(encoding="utf-8"))
            shared_audit = {
                **staged.iterations[0].audit,
                "checks": [check for check in staged.iterations[0].audit["checks"]
                           if check["检查项"] != "requirement_output_pending_review"],
            }
            # Literal mention cannot authorize content quality. Both entrypoints
            # retain technical checks and expose all planned requirements for review.
            expected_coverage = [{
                "audit_domain": "script_document", "检查项": "requirement_output_pending_review",
                "级别": "warning", "状态": "warning",
                "关联镜号": "、".join(str(value) for value in trace["plan_shot_nos"]),
                "详情": f"已规划要求‘{trace['brief_text']}’尚不能由当前成稿直接证明",
                "修复建议": "核对需求与当前镜头；确需改写时通过修订提案处理，事实待人工确认",
            } for trace in plan["traceability"]]
            for result in (legacy, staged):
                self.assertEqual(result.iterations[0].audit, add_audit_checks(shared_audit, expected_coverage))
            manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
            for stage_id in (
                "script_plan",
                "script_draft",
                "visual_binding",
                "workbook_render",
                "machine_audit",
            ):
                self.assertIn(
                    manifest["stages"][stage_id]["status"],
                    {"succeeded", "succeeded_with_warnings"},
                )

    def test_seventeen_shots_write_sixteen_adjacent_review_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "seventeen.xlsx"
            script_rows = [
                {"结构": f"结构{index}", "镜号": str(index)}
                for index in range(1, 18)
            ]
            detail_rows = [
                {"项目名称": "匿名发布会", "slot_index": index}
                for index in range(1, 18)
            ]
            write_template_output_workbook(
                output,
                script_rows,
                detail_rows,
                visual_asset_rows=[],
                embed_review_previews=False,
            )
            workbook = load_workbook(output, read_only=True)
            adjacent = workbook[ADJACENT_REVIEW_SHEET_NAME]
            self.assertEqual(adjacent.max_row - 1, 16)
            workbook.close()

    def _complete_prerequisites(self, state):
        refs = {}
        for stage_id in (
            "input_freeze",
            "source_catalog",
            "media_manifest",
            "visual_analysis",
        ):
            path = state.run_dir / f"{stage_id}.fixture"
            path.write_text(stage_id, encoding="utf-8")
            record = start_pipeline_stage(
                state,
                stage_id,
                input_sha256=f"input-{stage_id}",
                input_refs=tuple(refs.values()),
            )
            self.assertEqual(record["status"], "running")
            finish_pipeline_stage(
                state,
                stage_id,
                StageStatus.SUCCEEDED,
                output_sha256=sha256_file(path),
                output_refs=(str(path.resolve()),),
            )
            refs[stage_id] = str(path.resolve())

    @staticmethod
    def _slot(index):
        return SourceShot(
            script_type="发布会",
            source_file="匿名模板.xlsx",
            source_sheet="模版",
            source_row=index + 6,
            shot_no=str(index),
            structure=f"结构{index}",
            slot_index=index,
        )

    def _single_plan(self):
        slot = self._slot(1)
        return (
            [{"镜号": "1", "结构": "开场", "时长(秒)": "4"}],
            [{
                "slot_index": 1,
                "模板镜号": "1",
                "采用来源文件": slot.source_file,
                "采用来源 sheet": slot.source_sheet,
                "采用来源行": slot.source_row,
            }],
            [TemplateDecision(slot, None, "KEEP", 0, "保留模板")],
            [slot],
            {"项目名称": "匿名发布会"},
        )

    @staticmethod
    def _business_plan(
        duration="4",
        visual_desc="匿名项目主画面",
        sound="现场环境声",
        brief=None,
    ):
        slot = SourceShot(
            script_type="发布会",
            source_file="匿名模板.xlsx",
            source_sheet="模版",
            source_row=7,
            shot_no="1",
            structure="开场",
            duration_seconds=duration,
            visual_desc=visual_desc,
            sound=sound,
            slot_index=1,
        )
        return (
            [{
                "镜号": "1",
                "结构": "开场",
                "时长(秒)": duration,
                "画面描述": visual_desc,
                "声音": sound,
            }],
            [{
                "slot_index": 1,
                "模板镜号": "1",
                "采用来源文件": "匿名模板.xlsx",
                "采用来源 sheet": "模版",
                "采用来源行": 7,
            }],
            [TemplateDecision(slot, None, "KEEP", 0, "保留模板")],
            [slot],
            brief or {"项目名称": "匿名发布会"},
        )

    @staticmethod
    def _formal_confirmed(brief, *, certainty="confirmed"):
        formal_payload = {
            "facts": [
                {
                    "fact_id": f"fact_{index}", "field_path": field_name,
                    "value_json": json.dumps(value, ensure_ascii=False),
                    "certainty": certainty,
                    "evidence_refs": [{"artifact_ref": "artifact://evidence/anonymous"}],
                }
                for index, (field_name, value) in enumerate(brief.items(), start=1)
            ]
        }
        envelope = {"schema_version": FORMAL_BRIEF_SCHEMA, "workspace_id": "workspace-anonymous", "project_id": "project-anonymous", "brief_version_id": "brief-v1", "brief": brief}
        envelope["brief"] = formal_payload
        raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode()
        return (
            ConfirmedBriefInput(
                workspace_id="workspace-anonymous", project_id="project-anonymous", brief_version_id="brief-v1", confirmation_id="confirmation-v1", brief_snapshot_id="brief-snapshot-v1", payload=ArtifactPayload("artifact://brief/anonymous-v1.json", sha256(raw).hexdigest()), confirmation_status="confirmed", unresolved_conflict_count=0, confirmed_by_actor_id="actor-anonymous", confirmed_at="2026-08-13T00:00:00Z",
            ), raw,
        )

    def _confirmed_brief_dict(self):
        confirmed, _raw = self._formal_confirmed({"项目名称": "匿名发布会"})
        return confirmed.as_mapping()

    @staticmethod
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

    @staticmethod
    def _simple_renderer(item, target, audit_rows):
        workbook = Workbook()
        workbook.active.title = "脚本"
        workbook.save(target)
        workbook.close()


if __name__ == "__main__":
    unittest.main()
