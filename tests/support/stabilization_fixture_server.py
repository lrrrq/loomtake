"""Production composition, isolated storage, deterministic external providers.

An optional copied runtime config keeps pre-existing project history intact.
No endpoint, identity check, plan, task, version or review state is mocked.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

root = Path(os.environ["MPLUS_FORMAL_FIXTURE_ROOT"]).resolve()
root.mkdir(parents=True, exist_ok=True)
port = int(os.environ["MPLUS_FORMAL_TEST_PORT"])
snapshot_config = os.environ.get("MPLUS_STABILIZATION_SNAPSHOT_CONFIG")
os.environ.update(MPLUS_FORMAL_FIXTURE_MODE="deterministic",
    MPLUS_FORMAL_FIXTURE_ROOT=str(root / "fixture-preparation"))
from tests.support import formal_console_fixture_server as fixture
from tests import test_mvp_v2_formal_generation_service as providers
from tests.test_mvp_v2_handshake_creation import write_annual_template, SECTIONS, VISUALS
from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
from mvp_v2.formal_generation_service import FormalTextProviderPolicy
from mvp_v2.formal_runtime import build_formal_runtime
from mvp_v2.local_ui import create_app

if snapshot_config:
    config = json.loads(Path(snapshot_config).read_text(encoding="utf-8"))
else:
    annual = root / "fixture-preparation" / "M+年会类通用脚本库.xlsx"
    if not annual.exists():
        write_annual_template(annual)
    package = fixture.fixture_templates.import_template(category_id="annual",
        filename=annual.name, content=annual.read_bytes(), display_name="匿名年会25镜")
    fixture.fixture_template_registry.promote("annual", package["package_id"], package["package_version"])
    config = {"data_root": str(root / "data"), "runs_root": str(root / "runs"),
        "source_root": str(fixture.source_root), "asset_package": str(fixture.package_manifest),
        "asset_packages": {"launch": str(fixture.package_manifest), "annual": package["local_path"]},
        "package_registry_path": str(root / "fixture-preparation" / "fixture-template-registry.json"),
        "packages_root": str(root / "fixture-preparation" / "fixture-template-packages"),
        "formal_agent_read_only": False}
runtime = build_formal_runtime(config)
calls = []
provider = providers.FormalGenerationServiceTests("runTest")._provider("anonymous-stabilization", calls)
concurrent_formal = (root / "concurrent-formal.enabled").exists()
writer_barrier = threading.Barrier(2, timeout=30) if concurrent_formal else None
provider_log_lock = threading.Lock()

def text_provider(prompt, model, max_tokens):
    if concurrent_formal:
        # Only the external model boundary is controlled. Both real generation
        # requests must reach their writers before either can finish.
        with provider_log_lock:
            with (root / "formal-provider-calls.jsonl").open("a") as log:
                log.write(json.dumps({"model": model, "event": "entered", "time": time.time()}) + "\n")
        if model == "anonymous-writer":
            writer_barrier.wait()
    payload = json.loads(provider(prompt, model, max_tokens))
    source = json.loads(prompt.split("\nDATA:", 1)[1].split("\nDRAFT:", 1)[0])
    for shot, basis in zip(payload["shots"], source["shots"]):
        shot.update(basis.get("accepted_creative_fields") or {})
    return json.dumps(payload, ensure_ascii=False)

runtime.generation.provider_policy = FormalTextProviderPolicy(
    writer=text_provider, editor=text_provider,
    writer_provider="anonymous-stabilization", writer_model="anonymous-writer",
    editor_provider="anonymous-stabilization", editor_model="anonymous-editor")
runtime.brief_adapter.understanding_policy = None
runtime.brief_adapter.ocr_policy = None
runtime.agent.advisor = fixture.agent.advisor
LAUNCH_SECTIONS = ["现场空间", "来宾入场", "互动体验", "主舞台活动", "合影收束"]
LAUNCH_VISUALS = ["全景交代发布会现场空间", "来宾走过入口并入场",
    "参与者在互动体验区完成交流", "主舞台活动呈现台上动作与台下空间", "合影收束呈现集体合影与现场全景"]

def creative_stream(prompt, model, tokens, emit):
    context = json.loads(prompt.split("\n项目上下文：", 1)[1])
    duration = next((item['value'] for item in context['brief']['facts']
                     if item['field'] == '目标时长'), '60秒')
    seconds = 9 if duration == '45秒' else 12
    blocks = [{"段落": LAUNCH_SECTIONS[i], "画面描述": LAUNCH_VISUALS[i],
               "声音": "现场氛围与音乐推进", "时长": f"建议{seconds}秒"} for i in range(5)]
    text = json.dumps({"blocks": blocks}, ensure_ascii=False)
    for offset in range(0, len(text), 17):
        emit(text[offset:offset + 17])
    return text

def revision_provider(prompt, model, tokens):
    data = json.loads(prompt.split("\nDATA:", 1)[1])
    updates = json.loads(data["context"]["recent_conversation"][0]["content"].split("：", 1)[1])
    facts = {item["field"]: item["value"] for item in updates}
    indices = {shot["shot_id"]: int(shot["shot_no"]) - 1 for shot in data["selected_shots"]}
    operations = []
    for target in data["targets"]:
        index = indices[target["shot_id"]] % len(SECTIONS)
        values = {}
        if "脚本类型" in facts:
            if facts["脚本类型"] == "年会":
                values.update(structure=SECTIONS[index], visual=VISUALS[index])
            else:
                values.update(structure="发布会" + LAUNCH_SECTIONS[index], visual=LAUNCH_VISUALS[index])
        if "目标时长" in facts:
            values["duration_text"] = "12秒"
        if "风格调性" in facts:
            values["sound"] = "克制高级的音乐与现场环境声自然衔接"
        if target["field"] in values:
            operations.append({"shot_id": target["shot_id"], "field": target["field"], "value": values[target["field"]]})
    return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1",
                       "operations": operations}, ensure_ascii=False)

runtime.generation.creative_stream_provider = creative_stream
runtime.generation.creative_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
    invoke=revision_provider, provider="anonymous-stabilization", model="anonymous-revision")
app = create_app(agent_api=runtime.agent, formal_brief_adapter=runtime.brief_adapter,
    formal_brief_upload_factory=fixture._uploaded_document,
    formal_generation_service=runtime.generation, formal_script_service=runtime.script_workspace,
    formal_image_service=runtime.image_workspace, formal_review_service=runtime.review_workspace,
    formal_admin_service=runtime.admin)
(root / "runtime-evidence.json").write_text(json.dumps({
    "production_composition": True, "snapshot": bool(snapshot_config),
    "data_root": config["data_root"], "runs_root": config["runs_root"],
    "external_substitutions": ["script writer", "script editor", "creative stream", "creative revision", "agent advisor"],
    "brief_understanding": "canonical local parser", "business_state_injection": False,
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

if __name__ == "__main__":
    fixture.uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
