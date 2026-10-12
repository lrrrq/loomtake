"""Production Formal composition with an anonymous provider that fails once.

The shared deterministic fixture supplies genuine workbooks and image assets.
No recovery handler, task outcome, database row or browser state is fabricated.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ["MPLUS_FORMAL_FIXTURE_MODE"] = "deterministic"
root = Path(os.environ["MPLUS_FORMAL_FIXTURE_ROOT"]).resolve()
preparation_root = root / "fixture-preparation"
# Importing the shared fixture builds its own services. Its unused database and
# artifact cleanup must never share storage with the production runtime below.
os.environ["MPLUS_FORMAL_FIXTURE_ROOT"] = str(preparation_root)

from tests.support import formal_console_fixture_server as fixture
from mvp_v2.formal_generation_service import FormalTextProviderPolicy
from mvp_v2.formal_runtime import build_formal_runtime
from mvp_v2.local_ui import create_app


runtime = build_formal_runtime({
    "data_root": str(root / "data"),
    "runs_root": str(root / "runs"),
    "source_root": str(fixture.source_root),
    "asset_package": str(fixture.package_manifest),
    "asset_packages": {"launch": str(fixture.package_manifest)},
    "package_registry_path": str(preparation_root / "fixture-template-registry.json"),
    "packages_root": str(preparation_root / "fixture-template-packages"),
    "formal_agent_read_only": False,
})


def transient_provider(prompt: str, model: str, max_tokens: int) -> str:
    marker = root / "transient-provider-failure.json"
    if not marker.exists():
        marker.write_text(json.dumps({
            "status": "injected_transport_failure", "failure_count": 1,
            "injection_boundary": "configured script text provider",
        }) + "\n", encoding="utf-8")
        raise TimeoutError("anonymous fixture transient text provider timeout")
    return fixture._deterministic_provider(prompt, model, max_tokens)


runtime.generation.provider_policy = FormalTextProviderPolicy(
    writer=transient_provider, editor=transient_provider,
    writer_provider="anonymous-transient-provider", writer_model="anonymous-fixture-writer-model",
    editor_provider="anonymous-transient-provider", editor_model="anonymous-fixture-editor-model",
)
runtime.brief_adapter.understanding_policy = None
runtime.brief_adapter.ocr_policy = None
runtime.agent.advisor = fixture.agent.advisor

(root / "retry-fixture-mode.json").write_text(json.dumps({
    "mode": "deterministic", "production_composition": True,
    "recovery_handlers": "canonical runtime defaults",
    "fault": "first script text provider call raises TimeoutError",
    "business_state_injection": False,
    "runtime_data_root": str(root / "data"),
    "fixture_preparation_data_root": str(preparation_root / "data"),
}) + "\n", encoding="utf-8")

app = create_app(
    agent_api=runtime.agent,
    formal_brief_adapter=runtime.brief_adapter,
    formal_brief_upload_factory=fixture._uploaded_document,
    formal_generation_service=runtime.generation,
    formal_script_service=runtime.script_workspace,
    formal_image_service=runtime.image_workspace,
    formal_review_service=runtime.review_workspace,
    formal_admin_service=runtime.admin,
)


if __name__ == "__main__":
    fixture.uvicorn.run(app, host="127.0.0.1", port=fixture.TEST_PORT, log_level="warning")
