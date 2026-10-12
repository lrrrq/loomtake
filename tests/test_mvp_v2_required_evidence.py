import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from mvp_v2.audit import _source_supports_required_term, audit_generation
from mvp_v2.excel_io import read_text_brief_file
from mvp_v2.generator import generate_template_rows
from mvp_v2.models import SourceShot, TemplateDecision


def _slot(index: int, visual_desc: str = "") -> SourceShot:
    return SourceShot(
        script_type="发布会",
        source_file="template.xlsx",
        source_sheet="模版",
        source_row=index + 6,
        shot_no=str(index),
        structure=f"结构{index}",
        visual_desc=visual_desc,
        slot_index=index,
    )


def _generation_rows(description_by_slot=None, evidence_by_slot=None, source_content_by_slot=None):
    description_by_slot = description_by_slot or {}
    evidence_by_slot = evidence_by_slot or {}
    source_content_by_slot = source_content_by_slot or {}
    slots = [
        _slot(index, visual_desc=source_content_by_slot.get(index, ""))
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
            "画面描述": description_by_slot.get(slot.slot_index, "普通发布会画面"),
            "声音": "音乐",
        }
        for slot in slots
    ]
    detail_rows = []
    for slot in slots:
        detail = {
            "slot_index": slot.slot_index,
            "模板镜号": str(slot.slot_index),
            "替换方式": "KEEP",
            "模板来源文件": slot.source_file,
            "模板来源 sheet": slot.source_sheet,
            "模板来源行": slot.source_row,
            "采用来源类型": "模板",
            "采用来源文件": slot.source_file,
            "采用来源 sheet": slot.source_sheet,
            "采用来源行": slot.source_row,
            "采用来源证据": slot.visual_desc,
        }
        detail.update(evidence_by_slot.get(slot.slot_index, {}))
        detail_rows.append(detail)
    return slots, decisions, script_rows, detail_rows


def _brief(required_content: str) -> dict:
    return {
        "脚本类型": "发布会",
        "视频时长": "60s",
        "必须出现的内容": required_content,
    }


class RequiredContentEvidenceTests(unittest.TestCase):
    def test_file_backed_brief_is_valid_required_content_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief_path = Path(tmp) / "project-brief.txt"
            brief_path.write_text(
                "\n".join((
                    "项目名称：测试明星活动",
                    "项目区域文件名：测试明星活动.xlsx",
                    "脚本类型：明星活动",
                    "视频时长：90s",
                    "风格调性：高级克制",
                    "必须出现的内容：全场大合影",
                )),
                encoding="utf-8",
            )
            brief = read_text_brief_file(brief_path)
            slots, decisions, script_rows, detail_rows = _generation_rows(
                description_by_slot={1: "活动尾声完成全场大合影"},
            )
            detail_rows[0].update({
                key: brief[key]
                for key in (
                    "Brief来源文件",
                    "Brief来源 sheet",
                    "Brief来源行",
                    "Brief来源 SHA256",
                    "Brief来源证据",
                )
            })

            result = audit_generation(
                brief, slots, decisions, script_rows, detail_rows
            )

        self.assertNotIn("required_content_evidence_missing", result["blocking_items"])

    def test_changed_brief_file_cannot_be_used_as_required_content_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief_path = Path(tmp) / "project-brief.txt"
            brief_path.write_text(
                "\n".join((
                    "项目名称：测试明星活动",
                    "项目区域文件名：测试明星活动.xlsx",
                    "脚本类型：明星活动",
                    "视频时长：90s",
                    "风格调性：高级克制",
                    "必须出现的内容：全场大合影",
                )),
                encoding="utf-8",
            )
            brief = read_text_brief_file(brief_path)
            slots, decisions, script_rows, detail_rows = _generation_rows(
                description_by_slot={1: "活动尾声完成全场大合影"},
            )
            detail_rows[0].update({
                key: brief[key]
                for key in (
                    "Brief来源文件",
                    "Brief来源 sheet",
                    "Brief来源行",
                    "Brief来源 SHA256",
                    "Brief来源证据",
                )
            })
            brief_path.write_text(brief_path.read_text(encoding="utf-8") + "\n备注：已变化\n", encoding="utf-8")

            result = audit_generation(
                brief, slots, decisions, script_rows, detail_rows
            )

        self.assertIn("required_content_evidence_missing", result["blocking_items"])

    def test_file_backed_brief_required_content_is_written_without_library_literal(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief_path = Path(tmp) / "project-brief.txt"
            brief_path.write_text(
                "\n".join((
                    "项目名称：测试明星活动",
                    "项目区域文件名：测试明星活动.xlsx",
                    "脚本类型：明星活动",
                    "视频时长：90s",
                    "风格调性：高级克制",
                    "必须出现的内容：全场大合影",
                )),
                encoding="utf-8",
            )
            brief = read_text_brief_file(brief_path)
            slot = replace(_slot(1), script_type="明星活动", visual_desc="普通活动画面")
            decision = TemplateDecision(
                slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板"
            )

            script_rows, detail_rows = generate_template_rows(brief, [decision])

        self.assertIn("全场大合影", script_rows[0]["画面描述"])
        self.assertEqual(detail_rows[0]["Brief来源文件"], "project-brief.txt")

    def test_exhibition_brief_terms_accept_matching_approved_function_basis(self):
        self.assertTrue(_source_supports_required_term(
            "材料样品",
            "斜推圆形展台并切至牛仔面料、洗标和金属配件特写",
        ))
        self.assertTrue(_source_supports_required_term(
            "主题论坛",
            "发布舞台与满场观众正反打，主讲人宣讲时保留掌声",
        ))
        self.assertTrue(_source_supports_required_term(
            "可持续材料装置",
            "穿过入口进入展馆内部，连续展示空间装置与展台关系",
        ))
        self.assertFalse(_source_supports_required_term(
            "材料样品",
            "来宾从入口进入会场并完成签到",
        ))
        self.assertFalse(_source_supports_required_term(
            "主题论坛",
            "舞台空镜中品牌标识渐显",
        ))
    def test_text_and_source_trace_allow_missing_image_as_warning(self):
        slots, decisions, script_rows, detail_rows = _generation_rows(
            source_content_by_slot={1: "装置空镜，环形装置完整入画"},
        )

        result = audit_generation(
            _brief(""),
            slots,
            decisions,
            script_rows,
            detail_rows,
        )

        self.assertNotIn("missing_script_basis", result["blocking_items"])
        self.assertTrue(any(
            check["检查项"] == "missing_image"
            and check["级别"] == "warning"
            and check["audit_domain"] == "media_readiness"
            for check in result["checks"]
        ))

    def test_missing_text_reference_and_source_basis_is_hard_failure(self):
        slots, decisions, script_rows, detail_rows = _generation_rows()
        empty = replace(slots[0], structure="", visual_desc="")
        slots[0] = empty
        decisions[0] = replace(decisions[0], slot=empty)
        script_rows[0]["结构"] = ""
        detail_rows[0].update({
            "模板来源文件": empty.source_file,
            "模板来源 sheet": empty.source_sheet,
            "模板来源行": empty.source_row,
            "采用来源文件": empty.source_file,
            "采用来源 sheet": empty.source_sheet,
            "采用来源行": empty.source_row,
        })

        result = audit_generation(
            _brief(""),
            slots,
            decisions,
            script_rows,
            detail_rows,
        )

        self.assertIn("missing_script_basis", result["blocking_items"])
        check = next(
            item for item in result["checks"]
            if item["检查项"] == "missing_script_basis"
        )
        self.assertEqual(check["audit_domain"], "script_document")

    def test_required_term_written_into_script_without_evidence_is_hard_failure(self):
        slots, decisions, script_rows, detail_rows = _generation_rows(
            description_by_slot={1: "品牌logo 入画"},
        )

        result = audit_generation(
            _brief("品牌logo"),
            slots,
            decisions,
            script_rows,
            detail_rows,
        )

        self.assertIn("required_content_evidence_missing", result["blocking_items"])

    def test_required_term_with_trusted_visual_evidence_in_expected_slot_passes_evidence_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "logo.png"
            image_path.write_bytes(b"verified-image")
            evidence = {
                "image_id": "img_logo",
                "原图路径": str(image_path),
                "视觉读取状态": "minimax",
                "视觉内容摘要": "美的品牌logo特写",
                "视觉标签": "品牌；logo",
                "配图判断": "匹配",
                "slot_index": 1,
            }
            slots, decisions, script_rows, detail_rows = _generation_rows(
                description_by_slot={1: "品牌logo 入画"},
                evidence_by_slot={1: evidence},
            )

            result = audit_generation(
                _brief("品牌logo"),
                slots,
                decisions,
                script_rows,
                detail_rows,
                visual_asset_rows=[dict(evidence)],
            )

        self.assertNotIn("required_content_evidence_missing", result["blocking_items"])
        self.assertNotIn("required_content_slot_mismatch", result["blocking_items"])

    def test_approved_source_function_reference_can_support_required_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "source-reference.png"
            image_path.write_bytes(b"verified-source-reference")
            evidence = {
                "image_id": "img_esports_reference",
                "原图路径": str(image_path),
                "视觉读取状态": "minimax",
                "视觉内容摘要": "电竞比赛现场，选手在主舞台对抗",
                "视觉标签": "电竞；比赛；选手；对抗",
                "配图判断": "功能参考",
                "asset_role": "source_reference",
                "slot_index": 1,
            }
            slots, decisions, script_rows, detail_rows = _generation_rows(
                description_by_slot={1: "主舞台选手开始比赛对抗"},
                evidence_by_slot={1: evidence},
            )

            result = audit_generation(
                _brief("比赛对抗"),
                slots,
                decisions,
                script_rows,
                detail_rows,
                visual_asset_rows=[dict(evidence)],
            )

        self.assertNotIn("required_content_evidence_missing", result["blocking_items"])

    def test_required_term_with_semantically_supporting_source_reference_image_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / "approved-reference.png"
            reference.write_bytes(b"approved-reference")
            slots, decisions, script_rows, detail_rows = _generation_rows(
                description_by_slot={1: "品牌logo 入画"},
            )
            source = replace(
                slots[0],
                reference_image_path=str(reference),
                reference_image_evidence="源表嵌图已识别为品牌logo特写",
            )
            slots[0] = source
            decisions[0] = replace(decisions[0], slot=source)

            result = audit_generation(
                _brief("品牌logo"),
                slots,
                decisions,
                script_rows,
                detail_rows,
            )

        self.assertNotIn("required_content_evidence_missing", result["blocking_items"])

    def test_required_term_evidence_in_wrong_slot_is_hard_failure(self):
        slots, decisions, script_rows, detail_rows = _generation_rows(
            description_by_slot={9: "品牌logo 入画"},
            evidence_by_slot={
                9: {
                    "image_id": "img_logo",
                    "原图路径": "/tmp/logo.png",
                    "视觉读取状态": "minimax",
                    "视觉内容摘要": "品牌logo特写",
                    "视觉标签": "品牌；logo",
                    "配图判断": "匹配",
                }
            },
        )

        result = audit_generation(
            _brief("品牌logo"),
            slots,
            decisions,
            script_rows,
            detail_rows,
        )

        self.assertIn("required_content_slot_mismatch", result["blocking_items"])

    def test_authoritative_plan_binding_replaces_legacy_fixed_slot_guess(self):
        slots, decisions, script_rows, detail_rows = _generation_rows(
            description_by_slot={9: "品牌logo 入画"},
            evidence_by_slot={
                9: {
                    "image_id": "img_logo",
                    "原图路径": "/tmp/logo.png",
                    "视觉读取状态": "minimax",
                    "视觉内容摘要": "品牌logo特写",
                    "视觉标签": "品牌；logo",
                    "配图判断": "匹配",
                }
            },
        )
        detail_rows[8]["规划主事件"] = "品牌logo"

        result = audit_generation(
            _brief("品牌logo"), slots, decisions, script_rows, detail_rows,
        )

        self.assertNotIn("required_content_slot_mismatch", result["blocking_items"])

    def test_visual_writeback_caption_without_trusted_visual_fields_is_not_evidence(self):
        slots, decisions, script_rows, detail_rows = _generation_rows(
            description_by_slot={1: "品牌logo 入画"},
            evidence_by_slot={
                1: {
                    "视觉回写依据": "caption=品牌logo 入画",
                }
            },
        )

        result = audit_generation(
            _brief("品牌logo"),
            slots,
            decisions,
            script_rows,
            detail_rows,
        )

        self.assertIn("required_content_evidence_missing", result["blocking_items"])

    def test_keep_cannot_use_rejected_candidate_hit_as_template_source_evidence(self):
        slots, decisions, script_rows, detail_rows = _generation_rows(
            description_by_slot={1: "品牌logo 入画"},
            evidence_by_slot={
                1: {
                    "候选命中点": "品牌logo",
                    "匹配理由": "候选命中品牌logo但低于采用阈值",
                }
            },
        )

        result = audit_generation(
            _brief("品牌logo"),
            slots,
            decisions,
            script_rows,
            detail_rows,
        )

        self.assertIn("required_content_evidence_missing", result["blocking_items"])

    def test_adopted_source_evidence_passes_only_when_source_key_matches_decision(self):
        matching_evidence = {
            "采用来源类型": "模板",
            "采用来源文件": "template.xlsx",
            "采用来源 sheet": "模版",
            "采用来源行": 7,
            "采用来源证据": "品牌logo 从粒子中显现",
        }
        slots, decisions, script_rows, detail_rows = _generation_rows(
            description_by_slot={1: "品牌logo 入画"},
            evidence_by_slot={1: matching_evidence},
            source_content_by_slot={1: "品牌logo 从粒子中显现"},
        )

        result = audit_generation(
            _brief("品牌logo"),
            slots,
            decisions,
            script_rows,
            detail_rows,
        )
        self.assertNotIn("required_content_evidence_missing", result["blocking_items"])

        detail_rows[0]["采用来源行"] = 999
        mismatched = audit_generation(
            _brief("品牌logo"),
            slots,
            decisions,
            script_rows,
            detail_rows,
        )
        self.assertIn("required_content_evidence_missing", mismatched["blocking_items"])

    def test_forged_detail_source_evidence_cannot_replace_actual_source_for_any_mode(self):
        for mode in ("KEEP", "REPLACE", "ADAPT"):
            with self.subTest(mode=mode):
                slots, decisions, script_rows, detail_rows = _generation_rows(
                    description_by_slot={1: "品牌logo 入画"},
                    evidence_by_slot={1: {"采用来源证据": "品牌logo 伪造证据"}},
                )
                if mode != "KEEP":
                    variant = SourceShot(
                        script_type="发布会",
                        source_file="creative.xlsx",
                        source_sheet="创意库",
                        source_row=77,
                        slot_index=1,
                    )
                    decisions[0] = TemplateDecision(
                        slot=slots[0], variant=variant, mode=mode, score=90, match_reason="test"
                    )
                    detail_rows[0].update({
                        "替换方式": mode,
                        "采用来源类型": "创意库",
                        "采用来源文件": variant.source_file,
                        "采用来源 sheet": variant.source_sheet,
                        "采用来源行": variant.source_row,
                        "替换来源文件": variant.source_file,
                        "替换来源 sheet": variant.source_sheet,
                        "替换来源行": variant.source_row,
                    })

                result = audit_generation(
                    _brief("品牌logo"), slots, decisions, script_rows, detail_rows
                )

                self.assertIn("required_content_evidence_missing", result["blocking_items"])

    def test_visual_detail_is_not_evidence_without_matching_verified_asset(self):
        forged_visual = {
            "image_id": "img_fake",
            "原图路径": "/definitely/not/existing/logo.png",
            "视觉读取状态": "minimax",
            "视觉内容摘要": "品牌logo特写",
            "视觉标签": "logo",
            "配图判断": "匹配",
            "slot_index": 1,
        }
        slots, decisions, script_rows, detail_rows = _generation_rows(
            description_by_slot={1: "品牌logo 入画"},
            evidence_by_slot={1: forged_visual},
        )
        for visual_asset_rows in ([], [forged_visual]):
            with self.subTest(asset_supplied=bool(visual_asset_rows)):
                result = audit_generation(
                    _brief("品牌logo"),
                    slots,
                    decisions,
                    script_rows,
                    detail_rows,
                    visual_asset_rows=visual_asset_rows,
                )

                self.assertIn("required_content_evidence_missing", result["blocking_items"])

    def test_detail_mode_and_adopted_source_type_must_match_decision(self):
        slots, decisions, script_rows, detail_rows = _generation_rows()
        variant = SourceShot(
            script_type="发布会",
            source_file="creative.xlsx",
            source_sheet="创意库",
            source_row=77,
            slot_index=1,
        )
        decisions[0] = TemplateDecision(
            slot=slots[0], variant=variant, mode="REPLACE", score=90, match_reason="test"
        )
        detail_rows[0].update({"替换方式": "KEEP", "采用来源类型": "模板"})

        result = audit_generation(_brief(""), slots, decisions, script_rows, detail_rows)

        self.assertIn("decision_detail_mismatch", result["blocking_items"])

    def test_launch_slot_sets_must_be_unique_complete_and_decisions_aligned(self):
        for collection in ("template", "decision", "script", "detail", "decision_alignment"):
            with self.subTest(collection=collection):
                slots, decisions, script_rows, detail_rows = _generation_rows()
                if collection == "template":
                    slots[1] = slots[0]
                elif collection == "decision":
                    decisions[1] = TemplateDecision(
                        slot=slots[0], variant=None, mode="KEEP", score=0, match_reason="duplicate"
                    )
                elif collection == "script":
                    script_rows[1]["镜号"] = script_rows[0]["镜号"]
                elif collection == "detail":
                    detail_rows[1]["slot_index"] = detail_rows[0]["slot_index"]
                else:
                    wrong_slot = SourceShot(
                        script_type="发布会",
                        source_file="wrong-template.xlsx",
                        source_sheet="模版",
                        source_row=999,
                        shot_no="1",
                        structure=slots[0].structure,
                        slot_index=1,
                    )
                    decisions[0] = TemplateDecision(
                        slot=wrong_slot, variant=None, mode="KEEP", score=0, match_reason="misaligned"
                    )

                result = audit_generation(_brief(""), slots, decisions, script_rows, detail_rows)

                self.assertIn("invalid_launch_slot_mapping", result["blocking_items"])

    def test_keep_source_with_blocked_or_extended_reuse_level_is_hard_failure(self):
        for reuse_level in ("禁用", "禁用-客户要求", "不可复用"):
            with self.subTest(reuse_level=reuse_level):
                slots, decisions, script_rows, detail_rows = _generation_rows()
                blocked_slot = replace(slots[0], reuse_level=reuse_level)
                slots[0] = blocked_slot
                decisions[0] = TemplateDecision(
                    slot=blocked_slot,
                    variant=None,
                    mode="KEEP",
                    score=0,
                    match_reason="blocked template source",
                )

                result = audit_generation(_brief(""), slots, decisions, script_rows, detail_rows)

                self.assertIn("disabled_source_used", result["blocking_items"])

    def test_detail_row_order_and_source_mirrors_must_align_with_same_slot_decision(self):
        for mismatch in ("row_order", "template_shot", "adopted_source"):
            with self.subTest(mismatch=mismatch):
                slots, decisions, script_rows, detail_rows = _generation_rows()
                if mismatch == "row_order":
                    detail_rows[0], detail_rows[1] = detail_rows[1], detail_rows[0]
                elif mismatch == "template_shot":
                    detail_rows[0]["模板镜号"] = "2"
                else:
                    detail_rows[0]["采用来源行"] = slots[1].source_row

                result = audit_generation(_brief(""), slots, decisions, script_rows, detail_rows)

                self.assertTrue(
                    {"invalid_launch_slot_mapping", "decision_detail_mismatch"}
                    & set(result["blocking_items"])
                )

    def test_unknown_decision_mode_fails_closed(self):
        slots, decisions, script_rows, detail_rows = _generation_rows()
        decisions[0] = TemplateDecision(
            slot=slots[0],
            variant=None,
            mode="UNKNOWN",
            score=0,
            match_reason="invalid mode",
        )
        detail_rows[0]["替换方式"] = "UNKNOWN"

        result = audit_generation(_brief(""), slots, decisions, script_rows, detail_rows)

        self.assertIn("decision_detail_mismatch", result["blocking_items"])

    def test_replace_and_adapt_replacement_trace_must_equal_actual_variant(self):
        for mode in ("REPLACE", "ADAPT"):
            with self.subTest(mode=mode):
                slots, decisions, script_rows, detail_rows = _generation_rows()
                variant = SourceShot(
                    script_type="发布会",
                    source_file="creative.xlsx",
                    source_sheet="创意库",
                    source_row=77,
                    slot_index=1,
                )
                decisions[0] = TemplateDecision(
                    slot=slots[0],
                    variant=variant,
                    mode=mode,
                    score=90,
                    match_reason="selected variant",
                )
                detail_rows[0].update({
                    "替换方式": mode,
                    "替换来源文件": "forged.xlsx",
                    "替换来源 sheet": "伪造来源",
                    "替换来源行": 999,
                    "采用来源类型": "创意库",
                    "采用来源文件": variant.source_file,
                    "采用来源 sheet": variant.source_sheet,
                    "采用来源行": variant.source_row,
                })

                result = audit_generation(_brief(""), slots, decisions, script_rows, detail_rows)

                self.assertIn("replacement_source_mismatch", result["blocking_items"])

    def test_keep_ignores_unadopted_variant_reuse_and_missing_metadata(self):
        slots, decisions, script_rows, detail_rows = _generation_rows()
        rejected_variant = SourceShot(
            script_type="发布会",
            source_file="rejected.xlsx",
            source_sheet="创意库",
            source_row=88,
            reuse_level="禁用-客户要求",
            missing_metadata=("可复用等级", "禁用说明"),
            slot_index=1,
        )
        decisions[0] = TemplateDecision(
            slot=slots[0],
            variant=rejected_variant,
            mode="KEEP",
            score=20,
            match_reason="rejected variant remains for explanation",
        )

        result = audit_generation(_brief(""), slots, decisions, script_rows, detail_rows)

        self.assertNotIn("disabled_source_used", result["blocking_items"])
        self.assertFalse(any(
            check["检查项"] == "missing_reuse_metadata"
            and "rejected.xlsx" in check["详情"]
            for check in result["checks"]
        ))


if __name__ == "__main__":
    unittest.main()
