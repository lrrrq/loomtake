import tempfile
import unittest
from pathlib import Path

from PIL import Image as PILImage

from mvp_v2.audit import audit_generation, _required_term_present, _split_terms
from mvp_v2.brand_policy import conflicting_visual_brand_names, unknown_brand_mentions
from mvp_v2.models import SourceShot, TemplateDecision
from mvp_v2.visual_assets import attach_visual_assets, rematch_visual_assets


def _brief(forbidden: str = "烟酒") -> dict:
    return {
        "项目名称": "美的科技发布会",
        "品牌或客户": "美的",
        "脚本类型": "发布会",
        "视频时长": "60s",
        "必须出现的内容": "",
        "禁忌或不能出现的内容": forbidden,
    }


def _source(index: int, **overrides) -> SourceShot:
    values = {
        "script_type": "发布会",
        "source_file": "template.xlsx",
        "source_sheet": "模版",
        "source_row": index + 6,
        "shot_no": str(index),
        "structure": f"结构{index}",
        "visual_desc": "正常发布会画面",
        "slot_index": index,
    }
    values.update(overrides)
    return SourceShot(**values)


def _valid_launch():
    slots = [_source(index) for index in range(1, 19)]
    decisions = [
        TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0, match_reason="keep")
        for slot in slots
    ]
    script_rows = [
        {
            "结构": slot.structure,
            "镜号": slot.shot_no,
            "时长(秒)": 60 if slot.slot_index == 1 else 0,
            "画面描述": "正常发布会画面",
        }
        for slot in slots
    ]
    detail_rows = [
        {
            "slot_index": slot.slot_index,
            "模板镜号": slot.shot_no,
            "模板来源文件": slot.source_file,
            "模板来源 sheet": slot.source_sheet,
            "模板来源行": slot.source_row,
            "替换方式": "KEEP",
            "采用来源类型": "模板",
            "采用来源文件": slot.source_file,
            "采用来源 sheet": slot.source_sheet,
            "采用来源行": slot.source_row,
        }
        for slot in slots
    ]
    return slots, decisions, script_rows, detail_rows


def _bind_visual(detail: dict, image_path: Path, **overrides) -> dict:
    asset = {
        "slot_index": detail["slot_index"],
        "image_id": "img_01",
        "原图路径": str(image_path),
        "caption": "美的发布会主会场",
        "tags": ["美的", "发布会"],
        "brand_names": ["美的"],
        "ocr_text": "Midea",
        "视觉内容摘要": "美的发布会主会场",
        "negative_flags": [],
        "视觉读取状态": "minimax",
        "配图判断": "匹配",
    }
    asset.update(overrides)
    detail.update({
        "image_id": asset["image_id"],
        "原图路径": asset["原图路径"],
    })
    return asset


def _rematch_through_real_chain(
    tmp: str,
    script_rows: list[dict],
    detail_rows: list[dict],
    analysis_overrides: dict,
) -> list[dict]:
    original = Path(tmp) / "original.png"
    candidate = Path(tmp) / "candidate.png"
    PILImage.new("RGB", (1920, 1080), "white").save(original)
    PILImage.new("RGB", (1920, 1080), "blue").save(candidate)
    script_rows[0].update({
        "画面示意": str(original),
        "画面描述": "美的品牌 logo 清晰入画",
    })
    detail_rows, visual_assets = attach_visual_assets(script_rows, detail_rows)
    manifest = [{
        "image_id": "candidate-image",
        "原图路径": str(candidate),
        "来源文件": detail_rows[0]["模板来源文件"],
        "来源 sheet": detail_rows[0]["模板来源 sheet"],
        "来源行": detail_rows[0]["模板来源行"],
        "是否低清": False,
        "可复用等级": "可复用",
    }]
    analysis = {
        "image_id": "candidate-image",
        "caption": "美的品牌 logo 清晰入画",
        "tags": ["美的", "logo", "品牌标识"],
        "brand_names": ["美的"],
        "ocr_text": "Midea",
        "quality_flags": [],
        "negative_flags": [],
        "source": "minimax",
        **analysis_overrides,
    }
    _, detail_rows, visual_assets = rematch_visual_assets(
        script_rows,
        detail_rows,
        visual_assets,
        {},
        [candidate],
        manifest_rows=manifest,
        analysis_rows=[analysis],
        brief=_brief(),
    )
    return detail_rows, visual_assets


class DomainFinalGateTests(unittest.TestCase):
    def test_complete_copy_and_written_product_brand_keep_literal_content(self):
        required = '米白色直筒杯、同色杯盖、杯身写匿名；片尾完整字幕：带上匿名，轻松出发'
        self.assertEqual(_split_terms(required), [
            '米白色直筒杯', '同色杯盖', '杯身写匿名', '片尾完整字幕：带上匿名，轻松出发',
        ])
        self.assertTrue(_required_term_present('杯身写匿名', '杯身简单写“匿名”。'))
        self.assertFalse(_required_term_present('杯身写匿名', '杯身简单写“另一牌”。'))
        self.assertFalse(_required_term_present('杯身写匿名', '杯身不写“匿名”。'))
        copy = '片尾完整字幕：带上匿名，轻松出发'
        self.assertTrue(_required_term_present(copy, '合影后完整字幕“带上匿名，轻松出发”。'))
        self.assertFalse(_required_term_present(copy, '合影后字幕“带上匿名”。'))
        self.assertFalse(_required_term_present(copy, '合影，不出现字幕“带上匿名，轻松出发”。'))

    def test_generic_other_brand_exclusion_is_not_a_literal_brand_name(self):
        self.assertEqual(unknown_brand_mentions('匿名', '不出现任何其他品牌名字和标识。'), [])
        self.assertEqual(unknown_brand_mentions('匿名', '展示另一科技标识。'), ['另一科技'])

    def test_exclusions_in_accepted_prose_do_not_claim_forbidden_content_is_present(self):
        for prose, blocked in (("自然日光，不出现其他品牌。", False),
                               ("不出现未经确认的保温或容量参数。", False),
                               ("展示其他品牌和容量参数。", True),
                               ("展示无印良品与其他品牌。", True),
                               ("不出现其他品牌，但杯身印有其他品牌。", True)):
            with self.subTest(prose=prose):
                slots, decisions, rows, details = _valid_launch()
                rows[0]["画面描述"] = prose
                result = audit_generation(_brief("其他品牌、容量参数"), slots, decisions, rows, details, [])
                self.assertEqual("brief_forbidden_hit" in result["blocking_items"], blocked)
        slots, decisions, rows, details = _valid_launch()
        rows[0].update({"画面描述": "不出现其他品牌", "声音": "现场人员说出其他品牌"})
        result = audit_generation(_brief("其他品牌"), slots, decisions, rows, details, [])
        self.assertIn("brief_forbidden_hit", result["blocking_items"])

    def test_visual_provenance_marks_are_not_client_brand_conflicts(self):
        self.assertEqual(
            conflicting_visual_brand_names("美的", ["M+FILMS", "小红书"]),
            [],
        )
        self.assertEqual(
            conflicting_visual_brand_names("美的", ["M+FILMS", "小红书", "COLMO"]),
            ["COLMO"],
        )

    def test_generic_logo_appearance_is_not_parsed_as_an_unknown_brand_name(self):
        self.assertEqual(
            unknown_brand_mentions("美的", "Midea美的品牌标识特写，蓝底白字的品牌logo"),
            [],
        )
        self.assertEqual(
            unknown_brand_mentions("美的", "嘉宾手中拿着一杯印有蓝色logo的饮料杯"),
            [],
        )
        self.assertEqual(
            unknown_brand_mentions("林氏家居", "发布会现场展示品牌标识与广告语"),
            [],
        )
        self.assertEqual(
            unknown_brand_mentions("美的", "舞台背景为蓝色logo墙，灯光沿中轴展开"),
            [],
        )

    def test_launch_visual_chain_reports_missing_project_media_as_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            slots, decisions, script_rows, detail_rows = _valid_launch()
            image_path = Path(tmp) / "midea.png"
            image_path.write_bytes(b"image")
            asset = _bind_visual(detail_rows[0], image_path)

            result = audit_generation(
                _brief(), slots, decisions, script_rows, detail_rows, [asset]
            )

            self.assertNotIn("missing_bound_visual", result["blocking_items"])
            check = next(
                item for item in result["checks"]
                if item["检查项"] == "missing_bound_visual"
            )
            self.assertEqual(check["级别"], "warning")
            self.assertEqual(check["audit_domain"], "media_readiness")
            self.assertEqual(check["关联镜号"], "2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18")
            self.assertEqual(result["script_document_audit"]["status"], "pass")
            self.assertEqual(result["media_readiness"]["status"], "not_ready")

    def test_complete_eighteen_shot_control_has_no_domain_hard_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            slots, decisions, script_rows, detail_rows = _valid_launch()
            assets = []
            for index, detail in enumerate(detail_rows, start=1):
                image_path = Path(tmp) / f"midea-{index}.png"
                image_path.write_bytes(b"image")
                assets.append(_bind_visual(
                    detail,
                    image_path,
                    image_id=f"img_{index:02d}",
                ))

            result = audit_generation(
                _brief(), slots, decisions, script_rows, detail_rows, assets
            )

            self.assertEqual(result["hard_fail_count"], 0)

    def test_reference_visual_with_conflicting_brand_is_readiness_warning(self):
        conflicts = {
            "caption": "林氏家居发布会",
            "tags": ["林氏家居"],
            "brand_names": ["LINSY"],
            "ocr_text": "LINSY HOME",
            "视觉内容摘要": "林氏家居品牌墙",
        }
        for field, value in conflicts.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                slots, decisions, script_rows, detail_rows = _valid_launch()
                image_path = Path(tmp) / "linsy.png"
                image_path.write_bytes(b"image")
                asset = _bind_visual(detail_rows[0], image_path, **{field: value})

                result = audit_generation(
                    _brief(), slots, decisions, script_rows, detail_rows, [asset]
                )

                self.assertNotIn("bound_visual_brand_conflict", result["blocking_items"])
                check = next(
                    item for item in result["checks"]
                    if item["检查项"] == "bound_visual_brand_conflict"
                )
                self.assertEqual(check["级别"], "warning")
                self.assertEqual(check["audit_domain"], "media_readiness")

    def test_reference_visual_with_uncertain_brand_is_readiness_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            slots, decisions, script_rows, detail_rows = _valid_launch()
            image_path = Path(tmp) / "uncertain.png"
            image_path.write_bytes(b"image")
            asset = _bind_visual(
                detail_rows[0],
                image_path,
                brand_names=["未知", "美的"],
                ocr_text="",
            )

            result = audit_generation(
                _brief(), slots, decisions, script_rows, detail_rows, [asset]
            )

            self.assertNotIn("bound_visual_brand_unverified", result["blocking_items"])
            check = next(
                item for item in result["checks"]
                if item["检查项"] == "bound_visual_brand_unverified"
            )
            self.assertEqual(check["级别"], "warning")

    def test_unknown_brief_brand_records_reference_conflict_as_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            slots, decisions, script_rows, detail_rows = _valid_launch()
            image_path = Path(tmp) / "other-brand.png"
            image_path.write_bytes(b"image")
            asset = _bind_visual(
                detail_rows[0],
                image_path,
                caption="星河科技发布会现场",
                tags=["发布会", "品牌墙"],
                brand_names=["另一科技"],
                ocr_text="星河科技发布会",
                视觉内容摘要="星河科技发布会现场",
            )
            brief = _brief()
            brief["项目名称"] = "星河科技发布会"
            brief["品牌或客户"] = "星河科技"

            result = audit_generation(
                brief, slots, decisions, script_rows, detail_rows, [asset]
            )

            self.assertNotIn("bound_visual_brand_conflict", result["blocking_items"])
            self.assertTrue(any(
                item["检查项"] == "bound_visual_brand_conflict"
                and item["级别"] == "warning"
                for item in result["checks"]
            ))

    def test_adopted_source_forbidden_hit_warns_without_excluding_reference_source(self):
        slots, decisions, script_rows, detail_rows = _valid_launch()
        forbidden_slot = _source(1, visual_desc="烟酒陈列特写")
        slots[0] = forbidden_slot
        decisions[0] = TemplateDecision(
            slot=forbidden_slot, variant=None, mode="KEEP", score=0, match_reason="keep"
        )
        detail_rows[0].update({
            "模板来源文件": forbidden_slot.source_file,
            "模板来源 sheet": forbidden_slot.source_sheet,
            "模板来源行": forbidden_slot.source_row,
            "采用来源文件": forbidden_slot.source_file,
            "采用来源 sheet": forbidden_slot.source_sheet,
            "采用来源行": forbidden_slot.source_row,
        })

        result = audit_generation(_brief(), slots, decisions, script_rows, detail_rows)

        self.assertNotIn("brief_forbidden_source_hit", result["blocking_items"])
        self.assertTrue(any(
            item["检查项"] == "brief_forbidden_source_hit"
            and item["级别"] == "warning"
            for item in result["checks"]
        ))
        self.assertNotIn(
            {
                "source_file": forbidden_slot.source_file,
                "source_sheet": forbidden_slot.source_sheet,
                "source_row": forbidden_slot.source_row,
            },
            result["next_constraints"]["global_excludes"],
        )

    def test_reference_visual_forbidden_hit_is_readiness_warning(self):
        forbidden_fields = {
            "caption": "发布会烟酒礼品陈列",
            "tags": ["烟酒"],
            "ocr_text": "烟酒品牌",
            "negative_flags": ["烟酒"],
        }
        for field, value in forbidden_fields.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                slots, decisions, script_rows, detail_rows = _valid_launch()
                image_path = Path(tmp) / "forbidden.png"
                image_path.write_bytes(b"image")
                asset = _bind_visual(detail_rows[0], image_path, **{field: value})

                result = audit_generation(
                    _brief(), slots, decisions, script_rows, detail_rows, [asset]
                )

                self.assertNotIn("brief_forbidden_visual_hit", result["blocking_items"])
                check = next(
                    item for item in result["checks"]
                    if item["检查项"] == "brief_forbidden_visual_hit"
                )
                self.assertEqual(check["级别"], "warning")
                self.assertEqual(check["audit_domain"], "media_readiness")

    def test_unadopted_source_and_unbound_visual_do_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            slots, decisions, script_rows, detail_rows = _valid_launch()
            rejected = _source(
                1,
                source_file="rejected.xlsx",
                source_sheet="创意库",
                source_row=99,
                visual_desc="林氏家居烟酒陈列",
            )
            decisions[0] = TemplateDecision(
                slot=slots[0], variant=rejected, mode="KEEP", score=10, match_reason="rejected"
            )
            image_path = Path(tmp) / "unbound.png"
            image_path.write_bytes(b"image")
            unbound_asset = {
                "slot_index": 1,
                "image_id": "unbound",
                "原图路径": str(image_path),
                "caption": "林氏家居烟酒陈列",
                "tags": ["林氏家居", "烟酒"],
                "brand_names": ["LINSY"],
                "ocr_text": "LINSY HOME",
                "negative_flags": ["烟酒"],
            }

            result = audit_generation(
                _brief(), slots, decisions, script_rows, detail_rows, [unbound_asset]
            )

            self.assertNotIn("bound_visual_brand_conflict", result["blocking_items"])
            self.assertNotIn("brief_forbidden_source_hit", result["blocking_items"])
            self.assertNotIn("brief_forbidden_visual_hit", result["blocking_items"])

    def test_real_rematch_chain_rejects_conflicting_ocr_before_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            slots, decisions, script_rows, detail_rows = _valid_launch()
            detail_rows, visual_assets = _rematch_through_real_chain(
                tmp,
                script_rows,
                detail_rows,
                {"ocr_text": "LINSY HOME"},
            )

            self.assertNotEqual(detail_rows[0].get("image_id"), "candidate-image")
            self.assertNotIn("candidate-image", {
                str(asset.get("image_id") or "") for asset in visual_assets
            })

    def test_real_rematch_chain_preserves_negative_flags_as_readiness_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            slots, decisions, script_rows, detail_rows = _valid_launch()
            detail_rows, visual_assets = _rematch_through_real_chain(
                tmp,
                script_rows,
                detail_rows,
                {"negative_flags": ["烟酒"]},
            )

            result = audit_generation(
                _brief(), slots, decisions, script_rows, detail_rows, visual_assets
            )

            self.assertNotIn("brief_forbidden_visual_hit", result["blocking_items"])
            self.assertTrue(any(
                item["检查项"] == "brief_forbidden_visual_hit"
                and item["级别"] == "warning"
                for item in result["checks"]
            ))


if __name__ == "__main__":
    unittest.main()
