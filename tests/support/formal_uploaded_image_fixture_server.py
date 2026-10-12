"""Deterministic vision at the provider boundary; all browser actions use real cores."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

os.environ["MPLUS_FORMAL_FIXTURE_MODE"] = "deterministic"
from tests.support import formal_single_shot_library_fixture_server as gallery
from mvp_v2.formal_generation_service import FormalVisionProviderPolicy
from mvp_v2.image_understanding import PROJECT_IMAGE_VISION_PROMPT_VERSION, ProjectImageVisionResult

fixture = gallery.fixture


def understand_uploaded_image(path: Path) -> ProjectImageVisionResult:
    # This is a named, deterministic provider result, never a manual review row.
    record = {"provider": "anonymous-upload-vision-fixture", "model": "anonymous-upload-vision-v1",
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "status": "pass"}
    with (fixture.root / "uploaded-image-provider.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return ProjectImageVisionResult(
        status="pass", caption="嘉宾在会场内合影交流，嘉宾人物入画，呈现发布会现场人气。",
        tags=("发布会", "嘉宾合影", "嘉宾交流", "现场", "逻辑槽位3"),
        subjects=("嘉宾",), scene="匿名发布会现场嘉宾交流",
        purpose="开场造势；嘉宾人物入画", quality="匿名确定性识图 fixture",
        brand="画面中未见品牌标识", evidence=("deterministic provider fixture; not real model acceptance",),
    )


fixture.generation.vision_provider_policy = FormalVisionProviderPolicy(
    invoke=understand_uploaded_image, provider="anonymous-upload-vision-fixture",
    model="anonymous-upload-vision-v1", prompt_version=PROJECT_IMAGE_VISION_PROMPT_VERSION,
    max_batch_size=6,
)

if __name__ == "__main__":
    fixture.uvicorn.run(fixture.app, host="127.0.0.1", port=fixture.TEST_PORT, log_level="warning")
