"""Regressions for bounded, provenance-preserving image semantic requests."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp_v2.brand_policy import BRAND_ALIASES
from mvp_v2.image_retrieval import assess_image_candidates

ANONYMOUS_BRAND_ALIASES = {
    "星澜生活节": ("星澜生活节", "STARLAND LIFE FESTIVAL"),
    "晴川音乐节": ("晴川音乐节", "SUNRIVER FESTIVAL"),
}


class ApplicationSemanticPayloadTests(unittest.TestCase):
    @staticmethod
    def fixture(count: int, *, overview_repeats: int = 1):
        overview = "源表总览仅提供活动结构和调性，不能证明图片中存在指定品牌。" * overview_repeats
        rows, analyses = [], []
        for index in range(count):
            image_id = f"anonymous-image-{index:03d}"
            digest = sha256(image_id.encode()).hexdigest()
            source = {
                "source_file": "匿名音乐节模板.xlsx",
                "source_sheet": "创意库",
                "source_row": index // 3 + 7,
                "source_sha256": "a" * 64,
                "source_brand": "星澜生活节",
                "visual_desc": f"星澜生活节舞台歌手演唱，保留吉他和人物表情，来源段落{index // 3}。",
                "generic_desc": "歌手演唱画面",
                "shot_function": "演出建立",
                "sheet_overview": overview,
                "brand_judgement_original": "源案例文字声明为星澜生活节，尚未核对当前图片。",
            }
            rows.append({
                "image_id": image_id,
                "content_sha256": digest,
                "原图路径": f"/anonymous/{image_id}.png",
                "asset_role": "source_reference",
                "来源文件": source["source_file"],
                "来源 sheet": source["source_sheet"],
                "来源行": source["source_row"],
                "source_semantic_text": source["visual_desc"],
                "source_links": [deepcopy(source), deepcopy(source)],
                "package_sources": [{
                    "package_id": "anonymous-music",
                    "package_version": 3,
                    "package_manifest_sha256": "b" * 64,
                    "category_id": "music_festival",
                    "media_id": image_id,
                    "sources": [{"file": source["source_file"], "sheet": source["source_sheet"], "row": source["source_row"]}],
                }],
            })
            analyses.append({
                "image_id": image_id,
                "content_sha256": digest,
                "caption": f"中景记录歌手弹吉他演唱，背景舞台屏幕清晰显示晴川音乐节，画面编号{index}。",
                "tags": ["歌手", "吉他", "中景", "音乐节", f"画面{index}"],
                "brand_names": ["晴川音乐节"],
                "ocr_text": f"晴川音乐节 LIVE {index}",
                "negative_flags": [],
                "quality_flags": ["画面清晰"],
                "people_count": 1,
                "scene_type": "舞台演出",
                "shot_type": "中景",
                "action_type": "弹吉他唱歌",
                "asset_source_links": [deepcopy(source), deepcopy(source)],
                "source_file": f"/anonymous/metadata/{image_id}.json",
                "provider_endpoint": "https://example.invalid/private-provider-implementation",
                "annotator": "anonymous-annotator",
            })
        return rows, analyses, overview

    def capture(self, rows, analyses):
        calls = []

        def writer(prompt, model, max_tokens):
            calls.append((prompt, model, max_tokens))
            return json.dumps({"matches": []})

        slot = {
            "画面描述": "中景记录歌手在星澜生活节舞台演唱，保留吉他和人物表情。",
            "current_visual_authority": True,
        }
        policy = SimpleNamespace(writer=writer, writer_provider="fixture", writer_model="fixture")
        assessment = assess_image_candidates(slot, {"脚本类型": "音乐节"}, rows, analyses, policy)
        self.assertEqual(assessment["status"], "ready")
        self.assertEqual(len(calls), 1)
        prompt = calls[0][0]
        payload_text = prompt.split("\nINPUT:", 1)[1]
        return prompt, json.loads(payload_text), payload_text, slot

    def test_repeated_source_context_is_compacted_without_losing_visual_evidence(self):
        rows, analyses, overview = self.fixture(18, overview_repeats=40)
        _, payload, payload_text, slot = self.capture(rows, analyses)
        original = json.dumps({"slot": slot, "manifest": rows, "analyses": analyses}, ensure_ascii=False)
        self.assertLess(len(payload_text), len(original) // 2)
        self.assertLessEqual(payload_text.count(overview), 1)
        self.assertNotIn("private-provider-implementation", payload_text)
        self.assertNotIn("anonymous-annotator", payload_text)
        self.assertEqual(payload["slot"]["画面描述"], slot["画面描述"])
        self.assertEqual(len(payload["source_records"]), 6)
        self.assertIn("sheet_overview", payload["scope"]["omitted_metadata"])
        by_id = {item["image_id"]: item for item in analyses}
        self.assertGreaterEqual(len(payload["candidates"]), 8)
        for candidate in payload["candidates"]:
            original_analysis = by_id[candidate["image_id"]]
            self.assertNotIn("asset_source_links", candidate["visual_analysis"])
            self.assertEqual(candidate["content_sha256"], original_analysis["content_sha256"])
            self.assertEqual(len(candidate["source_record_ids"]), 1)
            source = payload["source_records"][candidate["source_record_ids"][0]]
            original_source = next(row for row in rows if row["image_id"] == candidate["image_id"])["source_links"][0]
            for key in ("source_file", "source_sheet", "source_row", "source_sha256", "visual_desc"):
                self.assertEqual(source[key], original_source[key])
            self.assertEqual(candidate["package_sources"][0]["package_manifest_sha256"], "b" * 64)
            for key in ("caption", "tags", "brand_names", "ocr_text", "negative_flags", "quality_flags"):
                self.assertEqual(candidate["visual_analysis"][key], original_analysis[key])

    def test_total_payload_budget_is_visible_when_inventory_exceeds_request_scope(self):
        rows, analyses, _ = self.fixture(120, overview_repeats=100)
        for analysis in analyses:
            analysis["caption"] += "现场镜头包含人物动作与设备细节，完整动作仍需复核。" * 100
            analysis["ocr_text"] += " 多段舞台字幕与活动日期，不能把字幕当成实物。" * 100
        _, payload, payload_text, slot = self.capture(rows, analyses)
        self.assertLessEqual(len(payload_text), 60000)
        self.assertGreaterEqual(len(payload["candidates"]), 8)
        self.assertLessEqual(len(payload["candidates"]), 96)
        scope = payload["scope"]
        self.assertEqual(scope["assessed_count"], len(payload["candidates"]))
        self.assertEqual(scope["safe_inventory_count"], 120)
        self.assertEqual(scope["shortlisted_count"], 96)
        self.assertEqual(scope["input_budget_characters"], 60000)
        self.assertGreater(scope["omitted_due_to_budget"], 0)
        self.assertEqual(scope["assessed_count"] + scope["omitted_due_to_budget"], scope["shortlisted_count"])
        self.assertTrue(scope["evidence_truncated"])
        self.assertEqual(payload["slot"]["画面描述"], slot["画面描述"])

    @patch.dict(BRAND_ALIASES, {}, clear=True)
    def test_current_slot_brand_requirement_applies_to_other_source_candidates(self):
        rows, analyses, _ = self.fixture(3)
        for row in rows[1:]:
            row["source_semantic_text"] = "晴川音乐节歌手弹吉他演唱。"
            for link in row["source_links"]:
                link["source_brand"] = "晴川音乐节"
                link["visual_desc"] = row["source_semantic_text"]
        _, payload, _, _ = self.capture(rows, analyses)
        self.assertEqual(len(payload["candidates"]), 3)
        for candidate in payload["candidates"]:
            self.assertEqual(candidate["requested_brands"], ["星澜生活节"])

    @patch.dict(BRAND_ALIASES, ANONYMOUS_BRAND_ALIASES, clear=True)
    def test_excluded_brand_is_not_required_or_used_to_cap_neutral_images(self):
        rows, analyses, _ = self.fixture(2)
        for analysis in analyses:
            analysis["caption"] = "中景记录歌手弹吉他，背景干净，没有品牌标识或文字。"
            analysis["tags"] = ["歌手", "吉他", "中景", "中性背景"]
            analysis["brand_names"] = []
            analysis["ocr_text"] = ""
        calls = []
        exclusion_hit = "没有星澜生活节标识，符合排除要求"

        def neutral_writer(prompt, *_):
            payload = json.loads(prompt.split("\nINPUT:", 1)[1])
            calls.append(payload)
            return json.dumps({"matches": [{
                "image_id": candidate["image_id"], "score": 94,
                "hits": ["中景歌手弹吉他", exclusion_hit],
                "missing": [], "conflicts": [],
                "reason": "歌手动作与景别一致，画面没有被排除的品牌标识。",
                "brand_checks": [],
            } for candidate in payload["candidates"]]}, ensure_ascii=False)

        assessment = assess_image_candidates(
            {"画面描述": "中景拍摄歌手弹吉他，画面不要出现星澜生活节标识。", "current_visual_authority": True},
            {"脚本类型": "音乐节"}, rows, analyses,
            SimpleNamespace(writer=neutral_writer, writer_provider="fixture", writer_model="fixture"),
        )
        self.assertEqual(assessment["status"], "ready")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(assessment["matches"]), 2)
        for candidate in calls[0]["candidates"]:
            self.assertEqual(candidate["requested_brands"], [])
            self.assertEqual(candidate["source_visual_conflicts"], [])
        for match in assessment["matches"]:
            self.assertEqual(match["score"], 94)
            self.assertEqual(match["missing"], [])
            self.assertEqual(match["conflicts"], [])
            self.assertIn(exclusion_hit, match["hits"])

    @patch.dict(BRAND_ALIASES, ANONYMOUS_BRAND_ALIASES)
    def test_source_case_brand_cannot_be_presented_as_observed_brand(self):
        rows, analyses, _ = self.fixture(3)
        prompt, payload, payload_text, _ = self.capture(rows, analyses)
        self.assertIn("星澜生活节", payload_text)
        self.assertIn("晴川音乐节", payload_text)
        for candidate in payload["candidates"]:
            observed = candidate["visual_analysis"]
            self.assertEqual(observed["brand_names"], ["晴川音乐节"])
            self.assertIn("晴川音乐节", observed["ocr_text"])
            self.assertNotIn("source_brand", observed)
            self.assertNotIn("brand_judgement_original", observed)
            self.assertNotIn("星澜生活节", json.dumps(observed, ensure_ascii=False))
            self.assertEqual(candidate["source_visual_conflicts"], [{
                "required_brand": "星澜生活节", "observed_other_brands": ["晴川音乐节"],
            }])
        instructions = prompt.split("\nINPUT:", 1)[0]
        self.assertIn("visual_analysis是唯一的图片已观察内容", instructions)
        self.assertIn("优先于原案例标题", instructions)
        self.assertIn("仅当本段明确要求品牌A而实际画面仅有品牌B", instructions)
        self.assertIn("严禁把A列为命中", instructions)

    @patch.dict(BRAND_ALIASES, ANONYMOUS_BRAND_ALIASES)
    def test_model_cannot_promote_source_brand_over_observed_conflicting_brand(self):
        rows, analyses, _ = self.fixture(2)

        def wrong_brand_writer(prompt, *_):
            payload = json.loads(prompt.split("\nINPUT:", 1)[1])
            return json.dumps({"matches": [{
                "image_id": candidate["image_id"], "score": 99,
                "hits": ["星澜生活节品牌标识", "歌手演唱"],
                "missing": [], "conflicts": [],
                "reason": "来源案例是星澜生活节，因此完全匹配。",
            } for candidate in payload["candidates"]]}, ensure_ascii=False)

        assessment = assess_image_candidates(
            {"画面描述": "星澜生活节舞台歌手弹吉他演唱。", "current_visual_authority": True},
            {"脚本类型": "音乐节"}, rows, analyses,
            SimpleNamespace(writer=wrong_brand_writer, writer_provider="fixture", writer_model="fixture"),
        )
        self.assertEqual(assessment["status"], "ready")
        self.assertEqual(len(assessment["matches"]), 2)
        for match in assessment["matches"]:
            self.assertLessEqual(match["score"], 54)
            self.assertTrue(all("星澜生活节" not in hit for hit in match["hits"]))
            self.assertTrue(any("星澜生活节" in item for item in match["missing"]))
            self.assertTrue(any("晴川音乐节" in item for item in match["conflicts"]))
            self.assertIn("功能参考", match["reason"])
            self.assertNotIn("完全匹配", match["reason"])

    @patch.dict(BRAND_ALIASES, {}, clear=True)
    def test_unregistered_bilingual_brand_is_not_capped_by_literal_name_difference(self):
        rows, analyses, _ = self.fixture(2)
        for analysis in analyses:
            analysis["brand_names"] = ["STARLAND LIFE FESTIVAL"]
            analysis["caption"] = "中景记录歌手弹吉他演唱，背景屏幕显示 STARLAND LIFE FESTIVAL。"
            analysis["ocr_text"] = "STARLAND LIFE FESTIVAL"
        seen = []

        def bilingual_writer(prompt, *_):
            payload = json.loads(prompt.split("\nINPUT:", 1)[1])
            seen.append(payload)
            return json.dumps({"matches": [{
                "image_id": candidate["image_id"], "score": 91,
                "hits": ["星澜生活节品牌标识", "歌手弹吉他演唱"],
                "missing": [], "conflicts": [],
                "reason": "画面英文活动名与当前段落的中文名称对应，歌手动作与景别一致。",
                "brand_checks": [{"requested_brand": "星澜生活节", "observed_name": "STARLAND LIFE FESTIVAL", "relation": "same"}],
            } for candidate in payload["candidates"]]}, ensure_ascii=False)

        assessment = assess_image_candidates(
            {"画面描述": "星澜生活节舞台歌手弹吉他演唱。", "current_visual_authority": True},
            {"脚本类型": "音乐节"}, rows, analyses,
            SimpleNamespace(writer=bilingual_writer, writer_provider="fixture", writer_model="fixture"),
        )
        self.assertEqual(assessment["status"], "ready")
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(assessment["matches"]), 2)
        for candidate in seen[0]["candidates"]:
            self.assertEqual(candidate["source_visual_conflicts"], [])
            self.assertEqual(candidate["visual_analysis"]["brand_names"], ["STARLAND LIFE FESTIVAL"])
        for match in assessment["matches"]:
            self.assertEqual(match["score"], 91)
            self.assertEqual(match["missing"], [])
            self.assertEqual(match["conflicts"], [])
            self.assertIn("星澜生活节品牌标识", match["hits"])

    @patch.dict(BRAND_ALIASES, {}, clear=True)
    def test_high_score_requires_observed_brand_match_even_for_unregistered_names(self):
        rows, analyses, _ = self.fixture(2)
        for relation, observed in (("different", "晴川音乐节"), ("missing", ""), ("uncertain", ""), (None, "")):
            with self.subTest(relation=relation):
                def unsupported_writer(prompt, *_):
                    payload = json.loads(prompt.split("\nINPUT:", 1)[1])
                    matches = []
                    for candidate in payload["candidates"]:
                        self.assertEqual(candidate["requested_brands"], ["星澜生活节"])
                        self.assertEqual(candidate["source_visual_conflicts"], [])
                        match = {"image_id": candidate["image_id"], "score": 99,
                                 "hits": ["星澜生活节品牌标识", "歌手弹吉他演唱"],
                                 "missing": [], "conflicts": [], "reason": "动作与景别一致。"}
                        if relation is not None:
                            match["brand_checks"] = [{"requested_brand": "星澜生活节",
                                                      "observed_name": observed, "relation": relation}]
                        matches.append(match)
                    return json.dumps({"matches": matches}, ensure_ascii=False)

                assessment = assess_image_candidates(
                    {"画面描述": "星澜生活节舞台歌手弹吉他演唱。", "current_visual_authority": True},
                    {"脚本类型": "音乐节"}, rows, analyses,
                    SimpleNamespace(writer=unsupported_writer, writer_provider="fixture", writer_model="fixture"),
                )
                self.assertEqual(assessment["status"], "ready")
                self.assertEqual(len(assessment["matches"]), 2)
                for match in assessment["matches"]:
                    self.assertLessEqual(match["score"], 54)
                    self.assertEqual(match["brand_checks"][0]["relation"], relation or "uncertain")
                    self.assertIn("星澜生活节", match["missing"])
                    self.assertTrue(all("星澜生活节" not in hit for hit in match["hits"]))
                    self.assertIn("功能参考", match["reason"])
                    if observed:
                        self.assertTrue(any(observed in item for item in match["conflicts"]))

    @patch.dict(BRAND_ALIASES, {}, clear=True)
    def test_invented_observed_brand_is_not_accepted_as_visual_evidence(self):
        rows, analyses, _ = self.fixture(2)

        def fabricated_writer(prompt, *_):
            payload = json.loads(prompt.split("\nINPUT:", 1)[1])
            return json.dumps({"matches": [{
                "image_id": candidate["image_id"], "score": 99, "hits": ["星澜生活节"],
                "missing": [], "conflicts": [], "reason": "品牌一致。",
                "brand_checks": [{"requested_brand": "星澜生活节", "observed_name": "STARLAND LIFE FESTIVAL", "relation": "same"}],
            } for candidate in payload["candidates"]]}, ensure_ascii=False)

        assessment = assess_image_candidates(
            {"画面描述": "星澜生活节舞台歌手弹吉他演唱。", "current_visual_authority": True},
            {"脚本类型": "音乐节"}, rows, analyses,
            SimpleNamespace(writer=fabricated_writer, writer_provider="fixture", writer_model="fixture"),
        )
        self.assertEqual(assessment["status"], "failed")
        self.assertEqual(assessment["matches"], [])

    @patch.dict(BRAND_ALIASES, {}, clear=True)
    def test_model_brand_check_can_anchor_to_current_slot_without_source_brand(self):
        rows, analyses, _ = self.fixture(2)
        for row in rows:
            for link in row["source_links"]:
                link.pop("source_brand")
        for analysis in analyses:
            analysis["brand_names"] = ["星澜"]
            analysis["caption"] = "2025星澜马拉松起跑门下，密集跑者正在集结。"
            analysis["ocr_text"] = "2025星澜马拉松"
        calls = []

        def anchored_writer(prompt, *_):
            payload = json.loads(prompt.split("\nINPUT:", 1)[1])
            calls.append(payload)
            return json.dumps({"matches": [{
                "image_id": candidate["image_id"], "score": 92,
                "hits": ["星澜品牌标识", "起跑门", "密集跑者"],
                "missing": [], "conflicts": [], "reason": "当前镜头与实际画面均明确出现星澜。",
                "brand_checks": [{"requested_brand": "星澜", "observed_name": "星澜", "relation": "same"}],
            } for candidate in payload["candidates"]]}, ensure_ascii=False)

        assessment = assess_image_candidates(
            {"画面描述": "航拍2025星澜马拉松起跑门与密集跑者方阵。", "current_visual_authority": True},
            {"脚本类型": "赛事"}, rows, analyses,
            SimpleNamespace(writer=anchored_writer, writer_provider="fixture", writer_model="fixture"),
        )
        self.assertEqual(assessment["status"], "ready")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(assessment["matches"]), 2)
        self.assertTrue(all(candidate["requested_brands"] == [] for candidate in calls[0]["candidates"]))
        for match in assessment["matches"]:
            self.assertEqual(match["score"], 92)
            self.assertEqual(match["brand_checks"], [{"requested_brand": "星澜", "observed_name": "星澜", "relation": "same"}])

    @patch.dict(BRAND_ALIASES, {}, clear=True)
    def test_model_cannot_add_brand_requested_only_by_source_case(self):
        rows, analyses, _ = self.fixture(2)
        for analysis in analyses:
            analysis["brand_names"] = ["星澜生活节"]
            analysis["caption"] = "星澜生活节舞台歌手弹吉他。"
            analysis["ocr_text"] = "星澜生活节"

        def source_only_writer(prompt, *_):
            payload = json.loads(prompt.split("\nINPUT:", 1)[1])
            return json.dumps({"matches": [{
                "image_id": candidate["image_id"], "score": 99,
                "hits": ["星澜生活节"], "missing": [], "conflicts": [], "reason": "沿用案例品牌。",
                "brand_checks": [{"requested_brand": "星澜生活节", "observed_name": "星澜生活节", "relation": "same"}],
            } for candidate in payload["candidates"]]}, ensure_ascii=False)

        assessment = assess_image_candidates(
            {"画面描述": "航拍马拉松起跑门与密集跑者方阵。", "current_visual_authority": True},
            {"脚本类型": "赛事"}, rows, analyses,
            SimpleNamespace(writer=source_only_writer, writer_provider="fixture", writer_model="fixture"),
        )
        self.assertEqual(assessment["status"], "failed")
        self.assertEqual(assessment["matches"], [])


if __name__ == "__main__":
    unittest.main()
