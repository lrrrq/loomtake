"""Real console services with a larger anonymous gallery for browser acceptance.

Only the input package is expanded.  The application, services, candidate
ranking, proposal execution, version save, review and delivery remain unchanged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image, ImageDraw

from mvp_v2.asset_package import create_asset_package, bundle_asset_package_v3
from mvp_v2.package_registry import build_package_record
# This fixture is intentionally self-contained even in a shell configured for
# a separate real-provider acceptance run.
for flag in ("MPLUS_FORMAL_REAL_DEEPSEEK", "MPLUS_FORMAL_REAL_MINIMAX", "MPLUS_FORMAL_REAL_ADVISOR_ONLY"):
    os.environ[flag] = "0"

from tests.support import formal_console_fixture_server as fixture


package = fixture.root / "single-shot-gallery-v3" / "asset_package.json"
if not package.is_file():
    gallery = fixture.root / "anonymous-gallery-input"
    gallery.mkdir(exist_ok=True)
    manifests, analyses = [], []
    for index in range(30):
        # Distinct shapes as well as colors prevent accidental static-content
        # grouping.  These are anonymous input fixtures, not photo quality UAT.
        image = Image.new("RGB", (320, 180), (index * 31 % 255, index * 53 % 255, 80))
        draw = ImageDraw.Draw(image)
        draw.rectangle((5 + index * 3, 20, 90 + index * 5, 150), fill=(240, 240 - index * 3, 150))
        draw.ellipse((170 - index, 10 + index, 280, 110 + index), fill=(35, index * 7 % 255, 180))
        draw.text((12, 155), f"Anonymous gallery {index:02d}", fill="white")
        path = gallery / f"anonymous-gallery-{index:02d}.png"
        image.save(path)
        image_id = f"anonymous-gallery-{index:02d}"
        manifests.append({"image_id": image_id, "原图路径": str(path)})
        analyses.append({"image_id": image_id,
            "caption": f"匿名图库{index:02d}：发布会产品展示台与观众互动交流",
            "tags": ["发布会", "产品演示", "观众互动", "现场"],
            "subjects": ["展台", "观众"], "scene": "匿名发布会现场",
            "purpose": "提供经人工选择的匿名构图参考", "brand_names": ["无"],
            "quality_flags": [], "vision_recognition_status": "succeeded",
            "review_status": "reviewed", "source": "anonymous_fixture"})
    for name, values in (("image_manifest.jsonl", manifests), ("image_analysis.minimax.jsonl", analyses)):
        (gallery / name).write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values), encoding="utf-8")
    if os.environ.get("MPLUS_SINGLE_SHOT_MULTI_SOURCE") == "1":
        from openpyxl import load_workbook
        from openpyxl.drawing.image import Image as ExcelImage
        source = load_workbook(fixture.source_workbook)
        for row in (7, 8):
            reference = ExcelImage(str(gallery / "anonymous-gallery-29.png"))
            source["模版"].add_image(reference, f"C{row}")
        source["模版"].add_image(ExcelImage(str(gallery / "anonymous-gallery-28.png")), "C7")
        source.save(fixture.source_workbook)
        source.close()
        fixture.refresh_ai_spec(fixture.source_workbook)
    seed = create_asset_package(fixture.root / "single-shot-gallery-seed",
        package_id="anonymous-single-shot-gallery", category_id="launch", source_root=fixture.source_root,
        media_roots=[fixture.fixture_dir, gallery], metadata_roots=[fixture.root / "asset-metadata", gallery])
    bundle_asset_package_v3(seed, package.parent, package_version=1)

# Existing service closures resolve this shared fixture input before any user
# project is created; all downstream selection/freeze behavior is production.
fixture.package_manifest = package
fixture.fixture_template_registry.register_records([build_package_record(package)], promote_formal=True)

if __name__ == "__main__":
    print(f"MPLUS_SINGLE_SHOT_GALLERY={package}", flush=True)
    fixture.uvicorn.run(fixture.app, host="127.0.0.1", port=fixture.TEST_PORT, log_level="warning")
