import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from PIL import Image

from mvp_v2.auto_pipeline import (
    AutoPipelineError,
    current_visual_cache_fingerprint,
    prepare_auto_pipeline_assets,
    project_cache_dir,
)
from mvp_v2.image_assets import build_image_manifest, read_jsonl, write_jsonl
from mvp_v2.image_understanding import VisionProviderError


class VisualCacheContractTests(unittest.TestCase):
    def test_manifest_growth_reuses_existing_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root, media, brief, cache_root = self._inputs(Path(tmp))
            cache_dir = project_cache_dir(brief, cache_root)
            initial_manifest = build_image_manifest(media, project_hint="测试项目")
            self._write_trusted_cache(
                cache_dir,
                initial_manifest,
                [self._analysis(initial_manifest[0]["image_id"])],
            )
            Image.new("RGB", (1920, 1080), color="black").save(media / "02.png")

            def add_only_pending(manifest_rows, output, **kwargs):
                existing = read_jsonl(output)
                done = {row["image_id"] for row in existing}
                pending = [row for row in manifest_rows if row["image_id"] not in done]
                self.assertEqual(len(pending), 1)
                write_jsonl(output, existing + [self._analysis(pending[0]["image_id"])])
                return 1

            with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=True):
                with patch(
                    "mvp_v2.auto_pipeline.write_minimax_analysis_incremental",
                    side_effect=add_only_pending,
                ):
                    assets = prepare_auto_pipeline_assets(
                        brief, source_root, cache_root, api_key=""
                    )

            self.assertEqual(assets.reused_analysis_count, 1)
            self.assertEqual(assets.generated_analysis_count, 1)

    def test_reviewed_creative_images_join_manifest_with_source_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root, media, brief, cache_root = self._inputs(Path(tmp))
            extracted = source_root / "_mvp_v2_extracted_images" / "M+发布会类通用脚本库" / "创意库"
            extracted.mkdir(parents=True)
            approved = extracted / "R48C4_1.png"
            blocked = extracted / "R49C4_1.png"
            Image.new("RGB", (1920, 1080), color="blue").save(approved)
            Image.new("RGB", (1920, 1080), color="red").save(blocked)
            review = Path(tmp) / "review.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "图片审核"
            sheet.append(["图片审核"])
            sheet.append([])
            sheet.append([])
            sheet.append([])
            sheet.append(["来源文件", "来源sheet", "来源行", "原图路径", "图片审核结论", "审核人", "审核日期"])
            sheet.append(["M+发布会类通用脚本库.xlsx", "创意库", 48, str(approved.relative_to(source_root)), "可用", "审核人", "2026-07-15"])
            sheet.append(["M+发布会类通用脚本库.xlsx", "创意库", 49, str(blocked.relative_to(source_root)), "禁用", "审核人", "2026-07-15"])
            workbook.save(review)
            workbook.close()

            def analyze_all(manifest_rows, output, **kwargs):
                write_jsonl(output, [self._analysis(row["image_id"]) for row in manifest_rows])
                return len(manifest_rows)

            with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=True):
                with patch(
                    "mvp_v2.auto_pipeline.write_minimax_analysis_incremental",
                    side_effect=analyze_all,
                ):
                    assets = prepare_auto_pipeline_assets(
                        brief,
                        source_root,
                        cache_root,
                        api_key="",
                        source_brand_review_path=review,
                    )

            paths = {Path(row["原图路径"]).name: row for row in assets.manifest_rows}
            self.assertIn("R48C4_1.png", paths)
            self.assertNotIn("R49C4_1.png", paths)
            self.assertEqual(paths["R48C4_1.png"]["来源文件"], "M+发布会类通用脚本库.xlsx")
            self.assertEqual(paths["R48C4_1.png"]["来源 sheet"], "创意库")
            self.assertEqual(paths["R48C4_1.png"]["来源行"], "48")

    def test_uncertain_brand_rows_are_complete_analysis_but_remain_untrusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root, media, brief, cache_root = self._inputs(Path(tmp))
            Image.new("RGB", (1920, 1080), color="black").save(media / "02.png")
            manifest = build_image_manifest(media, project_hint="测试项目")
            cache_dir = project_cache_dir(brief, cache_root)

            def write_one_invalid(manifest_rows, output, **kwargs):
                valid = self._analysis(manifest_rows[0]["image_id"])
                invalid = self._analysis(manifest_rows[1]["image_id"])
                invalid["brand_names"] = []
                write_jsonl(output, [valid, invalid])
                return 2

            with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=True):
                with patch(
                    "mvp_v2.auto_pipeline.write_minimax_analysis_incremental",
                    side_effect=write_one_invalid,
                ):
                    assets = prepare_auto_pipeline_assets(
                        brief, source_root, cache_root, api_key=""
                    )

            metadata = json.loads((cache_dir / "cache_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "complete")
            self.assertEqual(len(assets.analysis_rows), 2)
            uncertain = next(
                row for row in assets.analysis_rows if row["image_id"] == manifest[1]["image_id"]
            )
            self.assertEqual(uncertain["brand_names"], ["未知"])

    def test_partial_minimax_successes_are_reused_after_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root, media, brief, cache_root = self._inputs(Path(tmp))
            Image.new("RGB", (1920, 1080), color="black").save(media / "02.png")
            manifest = build_image_manifest(media, project_hint="测试项目")
            cache_dir = project_cache_dir(brief, cache_root)
            analysis_path = cache_dir / "image_analysis.minimax.jsonl"

            def partial_failure(manifest_rows, output, **kwargs):
                write_jsonl(output, [self._analysis(manifest_rows[0]["image_id"])])
                raise VisionProviderError("temporary failure")

            with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=True):
                with patch(
                    "mvp_v2.auto_pipeline.write_minimax_analysis_incremental",
                    side_effect=partial_failure,
                ):
                    with self.assertRaisesRegex(VisionProviderError, "temporary failure"):
                        prepare_auto_pipeline_assets(brief, source_root, cache_root, api_key="")

            self.assertTrue((cache_dir / "cache_meta.json").exists())
            self.assertEqual(len(read_jsonl(analysis_path)), 1)

            def finish_retry(manifest_rows, output, **kwargs):
                existing = read_jsonl(output)
                done = {row["image_id"] for row in existing}
                pending = [row for row in manifest_rows if row["image_id"] not in done]
                self.assertEqual(len(pending), 1)
                write_jsonl(output, existing + [self._analysis(pending[0]["image_id"])])
                return 1

            with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=True):
                with patch(
                    "mvp_v2.auto_pipeline.write_minimax_analysis_incremental",
                    side_effect=finish_retry,
                ):
                    assets = prepare_auto_pipeline_assets(brief, source_root, cache_root, api_key="")

            self.assertEqual(assets.reused_analysis_count, 1)
            self.assertEqual(assets.generated_analysis_count, 1)

    def test_matching_cache_fingerprint_reuses_api_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root, media, brief, cache_root = self._inputs(Path(tmp))
            cache_dir = project_cache_dir(brief, cache_root)
            manifest = build_image_manifest(media, project_hint="测试项目")
            self._write_trusted_cache(cache_dir, manifest, [self._analysis(manifest[0]["image_id"])])

            with patch("mvp_v2.auto_pipeline.write_minimax_analysis_incremental") as generate:
                assets = prepare_auto_pipeline_assets(brief, source_root, cache_root, api_key="")

            generate.assert_not_called()
            self.assertEqual(assets.cache_fingerprint, current_visual_cache_fingerprint())
            self.assertEqual(assets.reused_analysis_count, 1)

    def test_matching_fingerprint_rejects_tampered_analysis_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root, media, brief, cache_root = self._inputs(Path(tmp))
            cache_dir = project_cache_dir(brief, cache_root)
            manifest = build_image_manifest(media, project_hint="测试项目")
            analysis_path = cache_dir / "image_analysis.minimax.jsonl"
            self._write_trusted_cache(cache_dir, manifest, [self._analysis(manifest[0]["image_id"])])
            tampered = self._analysis(manifest[0]["image_id"])
            tampered["caption"] = "事后替换的伪造分析"
            write_jsonl(analysis_path, [tampered])

            with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=False):
                with self.assertRaisesRegex(AutoPipelineError, "完整性"):
                    prepare_auto_pipeline_assets(brief, source_root, cache_root, api_key="")

    def test_changed_cache_fingerprint_is_not_reused_without_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root, media, brief, cache_root = self._inputs(Path(tmp))
            cache_dir = project_cache_dir(brief, cache_root)
            manifest = build_image_manifest(media, project_hint="测试项目")
            analysis_path = cache_dir / "image_analysis.minimax.jsonl"
            original = self._analysis(manifest[0]["image_id"])
            write_jsonl(analysis_path, [original])
            (cache_dir / "cache_meta.json").write_text(
                json.dumps({"fingerprint": "stale-fingerprint"}),
                encoding="utf-8",
            )

            with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=False):
                with self.assertRaisesRegex(AutoPipelineError, "缓存指纹"):
                    prepare_auto_pipeline_assets(brief, source_root, cache_root, api_key="")

            self.assertEqual(
                json.loads(analysis_path.read_text(encoding="utf-8").splitlines()[0]),
                original,
            )

    def test_legacy_cache_without_metadata_is_not_trusted_without_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root, media, brief, cache_root = self._inputs(Path(tmp))
            cache_dir = project_cache_dir(brief, cache_root)
            manifest = build_image_manifest(media, project_hint="测试项目")
            write_jsonl(cache_dir / "image_analysis.minimax.jsonl", [self._analysis(manifest[0]["image_id"])])

            with patch("mvp_v2.auto_pipeline.minimax_credentials_available", return_value=False):
                with self.assertRaisesRegex(AutoPipelineError, "MiniMax 凭据"):
                    prepare_auto_pipeline_assets(brief, source_root, cache_root, api_key="")

            self.assertFalse((cache_dir / "cache_meta.json").exists())

    def test_legacy_cache_without_metadata_is_reanalyzed_with_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root, media, brief, cache_root = self._inputs(Path(tmp))
            cache_dir = project_cache_dir(brief, cache_root)
            manifest = build_image_manifest(media, project_hint="测试项目")
            image_id = manifest[0]["image_id"]
            write_jsonl(cache_dir / "image_analysis.minimax.jsonl", [self._analysis(image_id)])

            def regenerate(manifest_rows, analysis_path, **kwargs):
                row = self._analysis(manifest_rows[0]["image_id"])
                row["caption"] = "由当前 provider 重新分析"
                write_jsonl(analysis_path, [row])
                return 1

            with patch(
                "mvp_v2.auto_pipeline.write_minimax_analysis_incremental",
                side_effect=regenerate,
            ) as generate:
                assets = prepare_auto_pipeline_assets(
                    brief,
                    source_root,
                    cache_root,
                    api_key="current-key",
                )

            generate.assert_called_once()
            self.assertEqual(assets.generated_analysis_count, 1)
            self.assertEqual(assets.reused_analysis_count, 0)
            self.assertEqual(assets.analysis_rows[0]["caption"], "由当前 provider 重新分析")
            metadata = json.loads((cache_dir / "cache_meta.json").read_text(encoding="utf-8"))
            self.assertNotIn("migration", metadata)

    def test_cache_fingerprint_changes_with_provider_command_identity(self):
        self.assertNotEqual(
            current_visual_cache_fingerprint("mmx"),
            current_visual_cache_fingerprint("/opt/mmx-v2"),
        )

    def _inputs(self, root: Path):
        source_root = root / "整理后脚本库"
        media = source_root / "发布会类" / "测试项目"
        media.mkdir(parents=True)
        Image.new("RGB", (1920, 1080), color="white").save(media / "01.png")
        brief = {
            "项目名称": "测试发布会",
            "项目区域文件名": "测试项目",
            "素材目录或素材文件": str(media),
        }
        return source_root, media, brief, root / "assets"

    @staticmethod
    def _analysis(image_id: str) -> dict:
        return {
            "image_id": image_id,
            "caption": "测试发布会品牌logo",
            "tags": ["发布会", "logo"],
            "brand_names": ["无"],
            "source": "minimax",
            "analysis_provider": "minimax",
            "provenance_status": "api_generated",
        }

    @staticmethod
    def _write_trusted_cache(cache_dir: Path, manifest: list[dict], analysis: list[dict]) -> None:
        manifest_path = cache_dir / "image_manifest.jsonl"
        analysis_path = cache_dir / "image_analysis.minimax.jsonl"
        write_jsonl(manifest_path, manifest)
        write_jsonl(analysis_path, analysis)
        (cache_dir / "cache_meta.json").write_text(json.dumps({
            "fingerprint": current_visual_cache_fingerprint(),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "analysis_sha256": hashlib.sha256(analysis_path.read_bytes()).hexdigest(),
        }), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
