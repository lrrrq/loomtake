from __future__ import annotations

from pathlib import Path
import unittest

from mvp_v2.formal_console import ScriptRevisionProposalPayload
from mvp_v2.formal_default_images import _brand_label
from mvp_v2.formal_image_candidates import resolve_legacy_visual_identity
from mvp_v2.models import SourceShot
from mvp_v2.planning_core import _template_recommendation_summary
from tests.formal_uat_control_manifest import controls


class FormalUatReadinessContractTests(unittest.TestCase):
    def test_default_image_brand_uses_canonical_verdict_without_source_label_leak(self):
        self.assertEqual(_brand_label({"brand_names": ["无"]}, {}), "无")
        self.assertEqual(_brand_label({"brand_names": ["其他品牌"]}, {}), "其他品牌")
        self.assertEqual(_brand_label({}, {}), "未识别")
        self.assertNotEqual(_brand_label({}, {}), "类别资产包")

    def test_sixty_control_manifest_is_complete_and_bound_to_real_ui_selectors(self):
        manifest = controls()
        self.assertEqual([item["control_id"] for item in manifest], [
            f"M{index:02d}" for index in range(1, 61)
        ])
        self.assertEqual({item["group"] for item in manifest}, set(range(1, 7)))
        ui = "\n".join(
            (Path(__file__).parents[1] / "mvp_v2" / "ui" / name).read_text(encoding="utf-8")
            for name in ("index.html", "app.js")
        )
        for item in manifest:
            selector = item["selector"]
            marker = selector[1:] if selector.startswith("#") else selector.split("[")[-1].rstrip("]")
            marker = marker.split("=")[0]
            self.assertIn(marker, ui, item["control_id"])
            self.assertEqual(item["viewports"], [375, 1024, 1440])
            self.assertTrue(item["keyboard"])

    def test_zero_image_library_only_allows_previewed_candidates(self):
        import subprocess

        result = subprocess.run(["node", "-e", r"""
const assert = require('node:assert/strict');
const {harness} = require('./tests/support/workbench_clarity_probe.cjs');
(async () => {
  for (const mode of ['category_library_multi', 'category_library_browse']) {
    const {ctx} = harness('materials');
    ctx.state.formalImageProjectVersion = 4;
    ctx.state.formalDefaultImages = {selection_mode: mode, shots: [
      {shot_id: 'shot-1', shot_no: '1', selected_candidate_key: 'one'},
      {shot_id: 'shot-2', shot_no: '2', selected_candidate_key: 'two'},
    ]};
    assert.equal(ctx.formalDefaultAdoptionSelections().length, 0, 'Recommended images are not explicit selections');
    if (mode === 'category_library_multi') {
      ctx.state.formalDefaultLibrarySelected.set('one', {candidate_key: 'one'});
      ctx.state.formalDefaultLibrarySelected.set('two', {candidate_key: 'two'});
    } else {
      ctx.state.formalDefaultImageSelections.set('1', {candidate_key: 'one'});
      ctx.state.formalDefaultImageSelections.set('2', {candidate_key: 'two'});
    }
    const submitted = [];
    ctx.api = async (_path, options) => {
      submitted.push(JSON.parse(options.body));
      return {task_id: 'preview', response: {proposal_digest: 'digest'}};
    };
    await ctx.proposeFormalDefaultImages();
    assert.equal(submitted.length, 0, 'No selected image has a preview');
    ctx.state.formalImagePreviewUrls.set('default:unselected', 'blob:unselected');
    ctx.state.formalImagePreviewUrls.set('default:one', 'blob:one');
    await ctx.proposeFormalDefaultImages();
    assert.equal(submitted.length, 0, 'A missing selected preview cannot be replaced by another image');
    ctx.state.formalImagePreviewUrls.set('default:two', 'blob:two');
    await ctx.proposeFormalDefaultImages();
    assert.equal(submitted.length, 1, 'Every selected image now has a preview');
    assert.deepEqual(submitted[0].selections.map(item => item.candidate_key), ['one', 'two']);
    assert.deepEqual(submitted[0].selections.map(item => item.shot_no),
      mode === 'category_library_multi' ? ['library:1', 'library:2'] : ['1', '2']);
    if (mode === 'category_library_browse') {
      assert.deepEqual(submitted[0].allowed_shot_ids, ['shot-1', 'shot-2']);
    }
    ctx.state.formalImagePreviewUrls.delete('default:one');
    await ctx.proposeFormalDefaultImages();
    assert.equal(submitted.length, 1, 'A lost preview closes adoption again');
    ctx.state.formalImagePreviewUrls.set('default:one', 'blob:one');
    ctx.state.formalDefaultImageRequest += 1;
    await ctx.proposeFormalDefaultImages();
    assert.equal(submitted.length, 1, 'Old previews cannot authorize an in-flight gallery response');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""], cwd=Path(__file__).parents[1], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_revision_payload_requires_declared_entrypoint_and_feedback_kind(self):
        payload = ScriptRevisionProposalPayload.model_validate({
            "expected_project_version": 0,
            "expected_revision": 0,
            "instruction": "把第 1 镜的画面修改为匿名会场入口",
            "idempotency_key": "uat-history-copy-1",
            "entrypoint": "history",
            "feedback_kind": "copy",
        })
        self.assertEqual((payload.entrypoint, payload.feedback_kind), ("history", "copy"))

    def test_legacy_identity_exposes_stable_mapping_candidate_metadata(self):
        result = resolve_legacy_visual_identity(
            {"image_id": "media-stable"},
            [{
                "media_id": "media-stable", "content_sha256": "a" * 64,
                "path": "/controlled/media-stable.png",
            }],
        )
        self.assertEqual(result["candidate_badge"], "来自旧版本稳定映射")
        self.assertEqual(result["match_basis"], "media_id")

    def test_legacy_identity_accepts_only_sha_verified_exact_manifest_path(self):
        result = resolve_legacy_visual_identity(
            {"original_path": "/controlled/exact.png", "content_sha256": "b" * 64},
            [{
                "media_id": "media-exact", "content_sha256": "b" * 64,
                "path": "/controlled/exact.png",
            }],
        )
        self.assertEqual(result["resolution_kind"], "manifest_exact")
        rejected = resolve_legacy_visual_identity(
            {"original_path": "/other/exact.png"},
            [{
                "media_id": "media-exact", "content_sha256": "b" * 64,
                "path": "/controlled/exact.png",
            }],
        )
        self.assertEqual((rejected["status"], rejected["media_id"]), ("ambiguous", ""))

    def test_template_summary_lists_all_available_records_and_reuse(self):
        available = [
            SourceShot(
                script_type="发布会", source_file="匿名模板.xlsx", source_sheet="脚本",
                source_row=index, shot_no=str(index), shot_function=f"功能 {index}",
            )
            for index in range(1, 19)
        ]
        selected = [*available, available[0], available[1], available[1], available[2]]
        summary = _template_recommendation_summary(available, selected)
        self.assertEqual(summary["available_count"], 18)
        self.assertEqual(summary["selected_distinct_count"], 18)
        self.assertEqual(summary["reuse_count"], 4)
        self.assertEqual(len(summary["records"]), 18)
        self.assertEqual(summary["records"][1]["adopted_shot_nos"], ["2", "20", "21"])
        self.assertNotIn("sha", " ".join(summary["records"][0]).lower())


if __name__ == "__main__":
    unittest.main()
