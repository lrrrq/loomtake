import ast
import json
import unittest
from pathlib import Path

from mvp_v2.audit import _required_term_present, audit_generation
from mvp_v2.generator import (
    _ensure_generic_required_content,
    generate_script_rows,
    generate_template_rows,
    rewrite_template_rows_from_visual_evidence,
)
from mvp_v2.models import ScoredShot, SourceShot, TemplateDecision, validate_launch_brief
from mvp_v2.scoring import _score, _score_slot_variant, match_template_slots, score_candidates


def make_brief(**overrides):
    brief = {
        "项目名称": "测试发布会",
        "项目区域文件名": "测试发布会.xlsx",
        "脚本类型": "发布会",
        "品牌或客户": "测试品牌",
        "行业或活动类别": "科技发布会",
        "视频时长": "60s",
        "风格调性": "硬核科技感 高级 纯净",
        "必须出现的内容": "品牌logo",
        "禁忌或不能出现的内容": "竞品",
        "素材目录或素材文件": "/tmp/source",
    }
    brief.update(overrides)
    return brief


def source_shot(**overrides):
    data = {
        "script_type": "发布会",
        "source_file": "launch.xlsx",
        "source_sheet": "创意库",
        "source_row": 7,
        "shot_no": "1",
        "structure": "开场造势",
        "duration_seconds": "",
        "effect": "字体穿梭",
        "camera_type": "航拍",
        "shot_size": "全景",
        "camera_move": "航拍推进",
        "visual_desc": "品牌logo穿梭入画，建立活动主视觉",
        "sound": "科技感音乐",
        "note": "",
        "reference_image_path": "launch.xlsx#创意库!R7C4",
        "effect_reference_path": "",
        "reference_image_evidence": "",
        "effect_reference_evidence": "",
        "reuse_level": "",
        "forbidden_note": "",
        "tags": "",
        "shot_function": "",
        "visual_subject": "",
        "brand_neutral": True,
        "brand_neutral_evidence": "人工确认:测试夹具",
        "missing_metadata": (),
        "slot_index": 1,
    }
    data.update(overrides)
    return SourceShot(**data)


class RuleIntegrityTests(unittest.TestCase):
    def test_capability_registry_golden_tests_resolve_to_current_tests(self):
        repository_root = Path(__file__).resolve().parents[1]
        registry = json.loads(
            (repository_root / "docs/architecture/capabilities.json").read_text(
                encoding="utf-8"
            )
        )

        for capability in registry["capabilities"]:
            for reference in capability["golden_tests"]:
                with self.subTest(capability=capability["id"], reference=reference):
                    parts = reference.split(".")
                    self.assertEqual(len(parts), 4)
                    test_file = repository_root / "tests" / f"{parts[1]}.py"
                    self.assertTrue(test_file.is_file())
                    tree = ast.parse(test_file.read_text(encoding="utf-8"))
                    test_classes = {
                        node.name: {
                            member.name
                            for member in node.body
                            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                        }
                        for node in tree.body
                        if isinstance(node, ast.ClassDef)
                    }
                    self.assertIn(parts[2], test_classes)
                    self.assertIn(parts[3], test_classes[parts[2]])

    def test_natural_language_forbidden_terms_filter_source_candidates(self):
        brief = make_brief()
        brief["禁忌或不能出现的内容"] = "不要烟花、热气球和模特走秀"
        shots = [
            source_shot(source_row=11, visual_desc="热气球缓缓升起"),
            source_shot(source_row=12, visual_desc="团队在主舞台合影"),
        ]

        scored = score_candidates(brief, shots)

        self.assertEqual([item.shot.source_row for item in scored], [12])

    def test_match_template_slots_filters_blocked_materials_before_scoring(self):
        slot = source_shot(source_sheet="模版", source_row=3, slot_index=1)
        blocked = source_shot(
            source_row=8,
            visual_desc="品牌logo穿梭入画，普通外场空镜建立场地",
        )
        allowed = source_shot(
            source_row=9,
            visual_desc="品牌logo穿梭入画，主视觉转场切入舞台屏幕",
        )

        decisions = match_template_slots(make_brief(), [slot], [blocked, allowed])

        self.assertEqual(decisions[0].variant.source_row, 9)

    def test_match_template_slots_filters_forbidden_terms_before_scoring(self):
        slot = source_shot(source_sheet="模版", source_row=3, slot_index=1)
        forbidden = source_shot(
            source_row=8,
            visual_desc="品牌logo穿梭入画，地标空镜掠过城市夜景",
        )
        allowed = source_shot(
            source_row=9,
            visual_desc="品牌logo穿梭入画，主视觉转场切入舞台屏幕",
        )

        decisions = match_template_slots(make_brief(), [slot], [forbidden, allowed])

        self.assertEqual(decisions[0].variant.source_row, 9)

    def test_match_template_slots_hard_filters_requested_camera_fields(self):
        slot = source_shot(source_sheet="模版", source_row=3, slot_index=1)
        higher_wrong = source_shot(
            source_row=8,
            camera_type="航拍",
            shot_size="全景",
            visual_desc="品牌logo穿梭入画，主视觉转场切入舞台屏幕",
        )
        requested = source_shot(
            source_row=9,
            camera_type="地面机位",
            shot_size="特写",
            visual_desc="品牌logo特写后切入舞台屏幕",
        )

        decision = match_template_slots(
            make_brief(),
            [slot],
            [higher_wrong, requested],
            constraints={
                "required_by_slot": {
                    "1": {"camera_type": "地面机位", "shot_size": "特写"},
                },
            },
        )[0]

        self.assertIsNotNone(decision.variant)
        self.assertEqual(decision.variant.source_row, 9)

    def test_brand_transition_slot_rejects_ceremony_candidate(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=9,
            slot_index=3,
            structure="核心宣讲与仪式",
            visual_desc="品牌logo穿越机贴屏转场",
        )
        ceremony = source_shot(
            source_row=48,
            structure="核心宣讲与仪式",
            visual_desc="嘉宾在舞台完成颁发仪式",
        )
        brand_transition = source_shot(
            source_row=39,
            structure="核心宣讲与仪式",
            visual_desc="品牌logo贴屏后用穿越机俯冲切入主会场",
        )

        decision = match_template_slots(
            make_brief(), [slot], [ceremony, brand_transition]
        )[0]

        self.assertIsNotNone(decision.variant)
        self.assertEqual(decision.variant.source_row, 39)

    def test_generic_candidate_scoring_clamps_into_zero_to_hundred(self):
        high = source_shot(
            structure="品牌logo",
            effect="硬核科技感",
            shot_function="品牌logo",
            note="高级 纯净",
        )
        low = source_shot(
            structure="",
            effect="",
            shot_size="",
            camera_move="",
            reference_image_path="",
            visual_desc="普通素材",
        )

        high_score, _ = _score(make_brief(**{"结构": "品牌logo"}), high)
        low_score, _ = _score(make_brief(**{"结构": "完全不匹配"}), low)

        self.assertLessEqual(high_score, 100)
        self.assertGreaterEqual(high_score, 0)
        self.assertLessEqual(low_score, 100)
        self.assertGreaterEqual(low_score, 0)

    def test_slot_variant_scoring_clamps_into_zero_to_hundred(self):
        slot = source_shot(source_sheet="模版", source_row=3, slot_index=4, structure="核心宣讲与仪式")
        high = source_shot(
            source_row=8,
            slot_index=4,
            structure="核心宣讲与仪式",
            visual_desc="主会场空镜，舞台中心光束汇聚，硬核科技感",
            effect="字体穿梭",
            shot_size="全景",
            camera_move="航拍推进",
            reference_image_path="launch.xlsx#创意库!R8C4",
        )
        low = source_shot(
            source_row=9,
            slot_index=4,
            structure="核心宣讲与仪式",
            visual_desc="主持人报幕、观众手机录屏、茶歇外场空镜",
            effect="",
            shot_size="",
            camera_move="",
            reference_image_path="",
        )

        _, high_score, *_ = _score_slot_variant(
            make_brief(**{"必须出现的内容": "主会场", "风格调性": "硬核科技感"}),
            slot,
            high,
            18,
        )
        _, low_score, *_ = _score_slot_variant(
            make_brief(**{"必须出现的内容": "主会场", "风格调性": "硬核科技感"}),
            slot,
            low,
            18,
        )

        self.assertLessEqual(high_score, 100)
        self.assertGreaterEqual(high_score, 0)
        self.assertLessEqual(low_score, 100)
        self.assertGreaterEqual(low_score, 0)

    def test_validate_launch_brief_accepts_dynamic_positive_integer_duration(self):
        self.assertEqual(validate_launch_brief(make_brief(**{"视频时长": "60秒"})), 60)
        self.assertEqual(validate_launch_brief(make_brief(**{"视频时长": "60"})), 60)
        self.assertEqual(validate_launch_brief(make_brief(**{"视频时长": "56秒"})), 56)
        self.assertEqual(validate_launch_brief(make_brief(**{"视频时长": "90s"})), 90)

        for value in ("abc", "0", "-1", "56.5秒", "约60秒", "60 秒"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_launch_brief(make_brief(**{"视频时长": value}))

    def test_public_generation_entrypoints_reject_missing_or_blank_duration(self):
        slot = source_shot(source_sheet="模版", source_row=3, slot_index=1)
        scored = [ScoredShot(shot=source_shot(), score=90, match_reason="命中品牌logo")]
        decisions = [
            TemplateDecision(
                slot=slot,
                variant=None,
                mode="KEEP",
                score=0,
                match_reason="保留模板",
            )
        ]
        rewrite_script_rows = [{"画面描述": "普通发布会画面", "备注": "", "时长(秒)": 3}]
        rewrite_detail_rows = [{"slot_index": 1, "备注": ""}]

        briefs = [
            ("missing", {key: value for key, value in make_brief().items() if key != "视频时长"}),
            ("blank", make_brief(**{"视频时长": "   "})),
        ]
        for label, brief in briefs:
            with self.subTest(case=label, entrypoint="generate_script_rows"):
                with self.assertRaises(ValueError):
                    generate_script_rows(brief, scored)
            with self.subTest(case=label, entrypoint="generate_template_rows"):
                with self.assertRaises(ValueError):
                    generate_template_rows(brief, decisions)
            with self.subTest(case=label, entrypoint="rewrite_template_rows_from_visual_evidence"):
                with self.assertRaises(ValueError):
                    rewrite_template_rows_from_visual_evidence(
                        brief,
                        rewrite_script_rows,
                        rewrite_detail_rows,
                        [],
                    )

    def test_generate_script_rows_validates_duration_before_returning_empty_candidates(self):
        briefs = [
            ("missing", {key: value for key, value in make_brief().items() if key != "视频时长"}),
            ("blank", make_brief(**{"视频时长": "   "})),
            ("invalid", make_brief(**{"视频时长": "abc"})),
        ]

        for label, brief in briefs:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    generate_script_rows(brief, [])

    def test_audit_generation_rejects_invalid_launch_duration_strings(self):
        slots = [
            source_shot(
                source_sheet="模版",
                source_row=index + 6,
                shot_no=str(index),
                structure=f"结构{index}",
                slot_index=index,
            )
            for index in range(1, 19)
        ]
        decisions = [
            TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板")
            for slot in slots
        ]
        script_rows = [
            {
                "结构": slot.structure,
                "镜号": str(slot.slot_index),
                "时长(秒)": 60 if slot.slot_index == 1 else 0,
                "画面描述": "普通发布会画面",
                "声音": "音乐",
            }
            for slot in slots
        ]
        detail_rows = [
            {
                "slot_index": slot.slot_index,
                "模板镜号": str(slot.slot_index),
                "替换方式": "KEEP",
                "模板来源文件": slot.source_file,
                "模板来源 sheet": slot.source_sheet,
                "模板来源行": slot.source_row,
            }
            for slot in slots
        ]

        for value in ("abc", "0", "约60秒", "60 秒"):
            with self.subTest(value=value):
                result = audit_generation(
                    make_brief(**{"视频时长": value, "必须出现的内容": ""}),
                    slots,
                    decisions,
                    script_rows,
                    detail_rows,
                )

                self.assertEqual(result["status"], "fail")
                self.assertIn("invalid_launch_duration", result["blocking_items"])

    def test_audit_generation_reports_duration_and_count_mismatch_independently(self):
        slots = [
            source_shot(
                source_sheet="模版",
                source_row=index + 6,
                shot_no=str(index),
                structure=f"结构{index}",
                slot_index=index,
            )
            for index in range(1, 18)
        ]
        script_rows = [
            {
                "结构": slot.structure,
                "镜号": str(slot.slot_index),
                "时长(秒)": 3,
                "画面描述": "普通发布会画面",
                "声音": "音乐",
            }
            for slot in slots
        ]
        detail_rows = [
            {
                "slot_index": slot.slot_index,
                "模板镜号": str(slot.slot_index),
                "替换方式": "KEEP",
                "模板来源文件": slot.source_file,
                "模板来源 sheet": slot.source_sheet,
                "模板来源行": slot.source_row,
            }
            for slot in slots
        ]

        for value in ("abc", ""):
            with self.subTest(value=value):
                result = audit_generation(
                    make_brief(**{"视频时长": value}),
                    slots,
                    [],
                    script_rows,
                    detail_rows,
                )

                self.assertEqual(result["status"], "fail")
                self.assertIn("invalid_launch_duration", result["blocking_items"])
                self.assertIn("invalid_launch_slot_count", result["blocking_items"])

    def test_audit_generation_accepts_dynamic_positive_slot_counts(self):
        for count in (1, 16, 17, 19):
            with self.subTest(count=count):
                slots = [
                    source_shot(
                        source_sheet="模版",
                        source_row=index + 6,
                        shot_no=str(index),
                        structure=f"结构{index}",
                        slot_index=index,
                    )
                    for index in range(1, count + 1)
                ]
                decisions = [
                    TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板")
                    for slot in slots
                ]
                script_rows = [
                    {
                        "结构": slot.structure,
                        "镜号": str(slot.slot_index),
                        "时长(秒)": 60 if slot.slot_index == 1 else 0,
                        "画面描述": "普通发布会画面",
                        "声音": "音乐",
                    }
                    for slot in slots
                ]
                detail_rows = [
                    {
                        "slot_index": slot.slot_index,
                        "模板镜号": str(slot.slot_index),
                        "替换方式": "KEEP",
                        "模板来源文件": slot.source_file,
                        "模板来源 sheet": slot.source_sheet,
                        "模板来源行": slot.source_row,
                    }
                    for slot in slots
                ]

                result = audit_generation(
                    make_brief(**{"必须出现的内容": ""}),
                    slots,
                    decisions,
                    script_rows,
                    detail_rows,
                )

                self.assertNotIn("invalid_launch_slot_count", result["blocking_items"])

    def test_audit_generation_requires_all_rows_to_match_dynamic_template_count(self):
        slots = [
            source_shot(
                source_sheet="模版",
                source_row=index + 6,
                shot_no=str(index),
                structure=f"结构{index}",
                slot_index=index,
            )
            for index in range(1, 18)
        ]
        decisions = [
            TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板")
            for slot in slots
        ]
        script_rows = [
            {
                "结构": slot.structure,
                "镜号": str(slot.slot_index),
                "时长(秒)": 60 if slot.slot_index == 1 else 0,
                "画面描述": "普通发布会画面",
                "声音": "音乐",
            }
            for slot in slots
        ]
        detail_rows = [
            {
                "slot_index": slot.slot_index,
                "模板镜号": str(slot.slot_index),
                "替换方式": "KEEP",
                "模板来源文件": slot.source_file,
                "模板来源 sheet": slot.source_sheet,
                "模板来源行": slot.source_row,
            }
            for slot in slots
        ]

        cases = (
            ("decisions", decisions[:-1], script_rows, detail_rows),
            ("scripts", decisions, script_rows[:-1], detail_rows),
            ("details", decisions, script_rows, detail_rows[:-1]),
        )
        for label, case_decisions, case_scripts, case_details in cases:
            with self.subTest(case=label):
                result = audit_generation(
                    make_brief(**{"必须出现的内容": ""}),
                    slots,
                    case_decisions,
                    case_scripts,
                    case_details,
                )

                self.assertEqual(result["status"], "fail")
                self.assertIn("invalid_launch_slot_count", result["blocking_items"])

    def test_audit_generation_rejects_fractional_duration_without_truncation(self):
        slots = [
            source_shot(
                source_sheet="模版",
                source_row=index + 6,
                shot_no=str(index),
                structure=f"结构{index}",
                slot_index=index,
            )
            for index in range(1, 19)
        ]
        decisions = [
            TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板")
            for slot in slots
        ]
        script_rows = [
            {
                "结构": slot.structure,
                "镜号": str(slot.slot_index),
                "时长(秒)": 60.9 if slot.slot_index == 1 else 0,
                "画面描述": "普通发布会画面",
                "声音": "音乐",
            }
            for slot in slots
        ]
        detail_rows = [
            {"slot_index": slot.slot_index, "模板镜号": str(slot.slot_index), "替换方式": "KEEP"}
            for slot in slots
        ]

        result = audit_generation(
            make_brief(**{"必须出现的内容": ""}),
            slots,
            decisions,
            script_rows,
            detail_rows,
        )

        self.assertEqual(result["status"], "fail")
        self.assertIn("duration_total_mismatch", result["blocking_items"])

    def test_audit_generation_does_not_infer_launch_contract_from_row_count(self):
        slots = [
            source_shot(
                script_type="展会",
                source_sheet="模版",
                source_row=index + 6,
                shot_no=str(index),
                structure=f"结构{index}",
                slot_index=index,
            )
            for index in range(1, 19)
        ]
        decisions = [
            TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板")
            for slot in slots
        ]
        script_rows = [
            {"结构": slot.structure, "镜号": str(slot.slot_index), "画面描述": "展会现场"}
            for slot in slots
        ]
        detail_rows = [
            {"slot_index": slot.slot_index, "模板镜号": str(slot.slot_index), "替换方式": "KEEP"}
            for slot in slots
        ]

        for script_type in ("展会", "非发布会", ""):
            with self.subTest(script_type=script_type):
                result = audit_generation(
                    {"脚本类型": script_type, "必须出现的内容": ""},
                    slots,
                    decisions,
                    script_rows,
                    detail_rows,
                )

                self.assertNotIn("invalid_launch_duration", result["blocking_items"])
                self.assertNotIn("invalid_launch_slot_count", result["blocking_items"])

    def test_generate_template_rows_plans_required_term_and_marks_execution_evidence_pending(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=3,
            slot_index=1,
            visual_desc="蓝色能量线条快速收束成开场画面",
        )

        script_rows, detail_rows = generate_template_rows(
            make_brief(**{"必须出现的内容": "品牌logo"}),
            match_template_slots(make_brief(**{"必须出现的内容": "品牌logo"}), [slot], []),
        )

        self.assertIn("品牌logo", script_rows[0]["画面描述"])
        self.assertIn("Brief规划内容", detail_rows[0]["缺口说明"])
        self.assertIn("品牌logo", detail_rows[0]["缺口说明"])

    def test_generate_template_rows_does_not_report_gap_when_required_term_already_exists(self):
        slot = source_shot(
            source_sheet="模版",
            source_row=3,
            slot_index=1,
            visual_desc="品牌logo从蓝色粒子中显现并切入舞台",
        )

        script_rows, detail_rows = generate_template_rows(
            make_brief(**{"必须出现的内容": "品牌logo"}),
            match_template_slots(make_brief(**{"必须出现的内容": "品牌logo"}), [slot], []),
        )

        self.assertIn("品牌logo", script_rows[0]["画面描述"])
        self.assertNotIn("缺少来源/视觉证据：品牌logo", detail_rows[0]["缺口说明"])

    def test_generic_required_content_keeps_assignment_on_matching_function(self):
        script_rows = [
            {"结构": "应援互动", "画面描述": "观众席碰杯庆祝"},
            {"结构": "应援互动", "画面描述": "观众应援盛况"},
        ]
        detail_rows = [
            {"槽位功能": "产品饮用与干杯", "缺口说明": ""},
            {"槽位功能": "观众应援盛况", "缺口说明": ""},
        ]

        _ensure_generic_required_content(
            {"必须出现的内容": "观众应援"},
            script_rows,
            detail_rows,
        )

        self.assertNotIn("Brief规划内容", detail_rows[0]["缺口说明"])
        self.assertIn("Brief规划内容：观众应援", detail_rows[1]["缺口说明"])

    def test_generic_required_content_routes_competition_to_competition_arc(self):
        script_rows = [
            {"结构": "开场氛围", "画面描述": "邀请函对准入口前行"},
            {"结构": "竞技瞬间", "画面描述": "参赛者就位"},
        ]
        detail_rows = [
            {"槽位功能": "邀请函第一视角入场", "缺口说明": ""},
            {"槽位功能": "参赛者群像快切", "缺口说明": ""},
        ]

        _ensure_generic_required_content(
            {"必须出现的内容": "比赛对抗"},
            script_rows,
            detail_rows,
        )

        self.assertNotIn("Brief规划内容", detail_rows[0]["缺口说明"])
        self.assertIn("Brief规划内容：比赛对抗", detail_rows[1]["缺口说明"])

    def test_generic_required_content_keeps_spaces_inside_one_requirement(self):
        script_rows = [{"结构": "品牌落版", "画面描述": "企业LOGO居中定格完成落版"}]
        detail_rows = [{
            "槽位功能": "品牌KV落版",
            "采用来源证据": "企业LOGO与年度主题KV居中浮现",
            "缺口说明": "",
        }]

        _ensure_generic_required_content(
            {"必须出现的内容": "企业 Logo"},
            script_rows,
            detail_rows,
        )

        self.assertNotIn("画面完整呈现企业", script_rows[0]["画面描述"])
        self.assertIn("Brief规划内容：企业 Logo", detail_rows[0]["缺口说明"])

    def test_score_candidates_filters_disabled_reuse_and_brief_forbidden_before_scoring(self):
        allowed = source_shot(source_row=9, visual_desc="品牌logo穿梭入画")
        disabled = source_shot(source_row=10, reuse_level="禁用", visual_desc="品牌logo穿梭入画")
        forbidden = source_shot(source_row=11, visual_desc="品牌logo穿梭入画，竞品logo同屏")

        scored = score_candidates(make_brief(), [disabled, forbidden, allowed])

        self.assertEqual([item.shot.source_row for item in scored], [9])

    def test_score_candidates_fail_closed_for_disabled_or_unknown_reuse_values(self):
        blocked_values = ("禁用素材", "禁用（法务）", "禁止", "不可复用", "待确认")
        allowed = source_shot(source_row=9, reuse_level="镜头功能可复用", visual_desc="品牌logo穿梭入画")
        blocked = [
            source_shot(source_row=10 + index, reuse_level=value, visual_desc="品牌logo穿梭入画")
            for index, value in enumerate(blocked_values)
        ]

        scored = score_candidates(make_brief(), blocked + [allowed])

        self.assertEqual([item.shot.source_row for item in scored], [9])

    def test_slot_matching_fail_closed_for_disabled_or_unknown_reuse_values(self):
        slot = source_shot(source_sheet="模版", source_row=7, slot_index=1)
        blocked_values = ("禁用素材", "禁止", "不可复用", "待确认")
        blocked = [
            source_shot(source_row=10 + index, reuse_level=value, visual_desc="品牌logo穿梭入画")
            for index, value in enumerate(blocked_values)
        ]

        decisions = match_template_slots(make_brief(), [slot], blocked)

        self.assertEqual(decisions[0].mode, "KEEP")
        self.assertIsNone(decisions[0].variant)

    def test_compound_people_interaction_requires_people_and_interaction_semantics(self):
        for text in ("观众", "参会者陆续入场", "互动装置空镜"):
            with self.subTest(text=text):
                self.assertFalse(_required_term_present("人流互动", text))

        for text in (
            "观众与品牌装置互动", "参会者现场交流", "人流体验产品",
            "参观者在主题空间体验产品", "宾客在服务台交流",
        ):
            with self.subTest(text=text):
                self.assertTrue(_required_term_present("人流互动", text))

    def test_device_empty_shot_accepts_concrete_visual_device_aliases(self):
        for text in ("舞台中央发光圆形装置", "主会场装置艺术全景", "蓝色舞台装置"):
            with self.subTest(text=text):
                self.assertTrue(_required_term_present("装置空镜", text))

    def test_visual_rewrite_omits_platform_and_production_watermark_clauses(self):
        brief = make_brief(**{"视频时长": "60s"})
        script_rows = [{"画面描述": f"镜头{index}"} for index in range(1, 19)]
        detail_rows = [{"slot_index": index} for index in range(1, 19)]
        visual_assets = [{
            "slot_index": 4,
            "图片角色": "画面示意",
            "配图判断": "匹配",
            "视觉内容摘要": (
                "美的发布会弧形屏幕与蓝色舞台灯光，"
                "左上角小红书logo，右上角M+FILMS制作水印"
            ),
            "原图路径": "/tmp/stage.png",
            "image_id": "img_stage",
        }]

        rewritten, details, _ = rewrite_template_rows_from_visual_evidence(
            brief, script_rows, detail_rows, visual_assets
        )

        self.assertEqual(rewritten[3]["画面描述"], "镜头4")
        self.assertNotIn("小红书", rewritten[3]["画面描述"])
        self.assertNotIn("M+FILMS", rewritten[3]["画面描述"])
        self.assertIn("小红书", details[3]["视觉回写依据"])

    def test_core_launch_ceremony_rejects_weak_brand_claim_alias(self):
        for text in ("第一品牌", "发布会现场", "仪式感灯光"):
            with self.subTest(text=text):
                self.assertFalse(_required_term_present("核心发布仪式", text))

        for text in (
            "核心发布仪式", "启动仪式", "认证仪式", "颁发仪式",
            "颁奖仪式", "三位嘉宾在舞台中央合影颁奖",
        ):
            with self.subTest(text=text):
                self.assertTrue(_required_term_present("核心发布仪式", text))

    def test_required_content_understands_theme_installation_and_product_experience(self):
        self.assertTrue(
            _required_term_present(
                "主题装置",
                "舞台通道两侧装饰有城市天际线霓虹灯装置",
            )
        )
        self.assertTrue(
            _required_term_present(
                "产品体验",
                "四位年轻人围绕家居餐桌互动，呈现产品与主题空间体验",
            )
        )
        self.assertTrue(
            _required_term_present(
                "人流互动",
                "四个年轻人围着餐桌互动，现场气氛热闹",
            )
        )

    def test_compound_required_content_cannot_be_assembled_across_different_shots(self):
        slots = [
            source_shot(
                source_sheet="模版",
                source_row=index + 6,
                shot_no=str(index),
                structure=f"结构{index}",
                slot_index=index,
            )
            for index in range(1, 19)
        ]
        decisions = [
            TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板")
            for slot in slots
        ]
        script_rows = [
            {
                "结构": slot.structure,
                "镜号": str(slot.slot_index),
                "时长(秒)": 60 if slot.slot_index == 1 else 0,
                "画面描述": (
                    "嘉宾陆续入场" if slot.slot_index == 5
                    else "互动装置空镜" if slot.slot_index == 6
                    else "普通发布会画面"
                ),
            }
            for slot in slots
        ]
        detail_rows = [
            {"slot_index": slot.slot_index, "模板镜号": str(slot.slot_index), "替换方式": "KEEP"}
            for slot in slots
        ]

        result = audit_generation(
            make_brief(**{"必须出现的内容": "人流互动"}),
            slots,
            decisions,
            script_rows,
            detail_rows,
        )

        self.assertIn("required_content_missing", result["blocking_items"])


if __name__ == "__main__":
    unittest.main()
