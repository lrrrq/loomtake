"""Real local composition for legacy asset and knowledge browser journeys.

Browser file selection supplies anonymous input bytes. Asset uploads,
confirmations, review, publication and knowledge governance use production
services and persistent databases. No model is required by this journey.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

root = Path(os.environ["MPLUS_FORMAL_CONTROL_LEGACY_ROOT"]).resolve()
root.mkdir(parents=True, exist_ok=True)
runtime_root = root / "runtime"
source_root = root / "source"
source_root.mkdir(exist_ok=True)
port = int(os.environ["MPLUS_UI_TEST_PORT"])
for key in ("DEEPSEEK_API_KEY", "MINIMAX_API_KEY"):
    os.environ.pop(key, None)
os.environ.update({
    "MPLUS_CONFIG": str(root / "config.json"),
    "MPLUS_DATA_ROOT": str(runtime_root),
    "MPLUS_SOURCE_ROOT": str(source_root),
    "MPLUS_PORT": str(port),
    "MPLUS_SECRETS": str(root / "unused-secrets.env"),
    "MPLUS_ASSET_PACKAGE": str(root / "package" / "asset_package.json"),
    "MPLUS_PACKAGE_REGISTRY": str(runtime_root / "package_registry.json"),
    "MPLUS_FORMAL_AGENT_READ_ONLY": "0",
    "MPLUS_PROFILE": "test",
})

import uvicorn
from PIL import Image
from mvp_v2 import local_ui
from mvp_v2.ai_spec import refresh_ai_spec
from mvp_v2.asset_package import bundle_asset_package_v3, create_asset_package
from mvp_v2.formal_runtime import build_formal_runtime, uploaded_document
from mvp_v2.package_registry import LocalPackageRegistry, build_package_record
from tests.test_mvp_v2_ui_governance import _write_source_workbook

workbook = source_root / "M+发布会类通用脚本库.xlsx"
if not workbook.exists():
    _write_source_workbook(workbook)
    report = refresh_ai_spec(workbook)
    if not report.in_sync or not report.usable:
        raise AssertionError("匿名模板 AI 规范未通过")
manifest = root / "package" / "asset_package.json"
if not manifest.exists():
    original = create_asset_package(
        root / "package-source", package_id="anonymous-legacy-launch",
        source_root=source_root, category_id="launch",
    )
    bundle_asset_package_v3(original, manifest.parent, package_version=1)
registry = LocalPackageRegistry(runtime_root / "package_registry.json")
registry.register_records([build_package_record(manifest)], promote_formal=True)
(root / "config.json").write_text(json.dumps({
    "asset_package": str(manifest),
    "asset_packages": {"发布会": str(manifest)},
    "formal_agent_read_only": False,
}, ensure_ascii=False), encoding="utf-8")
image_path = root / "anonymous-reference.png"
if not image_path.exists():
    Image.new("RGB", (320, 180), (47, 102, 141)).save(image_path)

formal = build_formal_runtime()
app = local_ui.create_app(
    agent_api=formal.agent,
    formal_brief_adapter=formal.brief_adapter,
    formal_brief_upload_factory=uploaded_document,
    formal_generation_service=formal.generation,
    formal_script_service=formal.script_workspace,
    formal_image_service=formal.image_workspace,
    formal_review_service=formal.review_workspace,
    formal_admin_service=formal.admin,
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
