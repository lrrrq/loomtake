"""Server-side composition for the formal M+ product journey.

This module owns wiring only.  It never exposes provider credentials or model
controls to employee routes and it never performs a model call at startup.
Startup may consume persisted recovery events through registered stage handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import BinaryIO, Callable, Mapping

from .asset_package import asset_package_source_record, load_asset_package
from .asset_storage import LocalAssetStorage
from .package_publication import AssetPackagePublicationService
from .cli import DEFAULT_SOURCE_ROOT
from .formal_brief_input_adapter import (
    FormalBriefInputAdapter,
    FormalBriefUnderstandingPolicy,
    UploadedDocument,
)
from .formal_brief_workspace_service import FormalBriefWorkspaceService
from .formal_brief_domain import FormalBriefOcrPolicy
from .brief_artifact_store import BriefArtifactStore, load_or_create_confirmation_key
from .action_confirmation import ActionConfirmationService
from .tool_idempotency import ToolIdempotencyService
from .executable_agent import ExecutableAgentServices
from .formal_advisor import FormalAdvisorPolicy, FormalAdvisorService
from .formal_agent import FormalExecutableAgentService, FormalExecutionBackend
from .formal_admin_service import FormalAdminService
from .formal_recovery_executor import FormalRecoveryExecutor, RecoveryStageHandler
from .formal_generation_service import (
    FormalGenerationService,
    FormalGenerationServices,
    FormalTextProviderPolicy,
    FormalVisionProviderPolicy,
)
from .formal_image_workspace import (
    FormalImageWorkspaceService,
    FormalImageWorkspaceServices,
)
from .formal_review_workspace import (
    FormalReviewWorkspaceService,
    FormalReviewWorkspaceServices,
    WorkspaceBoundFinalConfirmation,
    WorkspaceBoundVersionQuality,
    WorkspaceReviewerActorResolver,
)
from .formal_script_workspace import (
    FormalScriptWorkspaceService,
    FormalScriptWorkspaceServices,
)
from .formal_script_revision import FormalScriptRevisionService
from .formal_script_assistant import FormalScriptAssistantEditorPolicy
from .formal_default_images import FormalDefaultImageService
from .manual_review_commands import ManualReviewCommandService, ManualReviewCommandServices
from .image_understanding import (
    PROJECT_IMAGE_VISION_PROMPT_VERSION,
    generate_minimax_project_image_analysis,
    _run_minimax_vision,
)
from .project_image_authority import ProjectImagePoolService
from .readonly_agent import ReadonlyAgentService
from .review_evidence_artifacts import FileReviewEvidenceArtifactStore, ReviewEvidenceAuthority
from .runtime_config import load_runtime_config
from .package_registry import build_package_record, LocalPackageRegistry, PackageRegistryError
from .script_type_adapter import category_id_for_script_type, normalize_script_type, SCRIPT_TYPE_METADATA
from .text_providers import build_text_provider, deepseek_http_stream_provider, provider_credentials_available
from .version_delivery import VersionDeliveryService


DEEPSEEK_PROVIDER = "deepseek-http"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_WRITER_MAX_OUTPUT_TOKENS = 8192
DEEPSEEK_EDITOR_MAX_OUTPUT_TOKENS = 8192
DEEPSEEK_BRIEF_MAX_OUTPUT_TOKENS = 8192
DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS = 4096
MINIMAX_VISION_PROVIDER = "minimax-cli"
MINIMAX_VISION_MODEL = "mmx-vision-describe/provider-managed"


def formal_agent_read_only(runtime: dict) -> bool:
    """Only an explicit deployment setting disables Formal Agent writes."""
    configured = runtime.get("formal_agent_read_only")
    if configured is not None:
        return bool(configured)
    return os.environ.get("MPLUS_FORMAL_AGENT_READ_ONLY") == "1"


@dataclass(frozen=True)
class FormalCapabilityRoute:
    capability: str
    role: str
    provider: str
    model: str
    available: bool


class FormalCapabilityRouter:
    """Server-owned provider choice; employee requests never select a model."""

    def __init__(self):
        deepseek_ready = provider_credentials_available(DEEPSEEK_PROVIDER)
        minimax_ready = provider_credentials_available(MINIMAX_VISION_PROVIDER)
        self._routes = {
            "brief_text_understanding": FormalCapabilityRoute(
                "brief_text_understanding", "资料文字理解",
                DEEPSEEK_PROVIDER, DEEPSEEK_MODEL, deepseek_ready,
            ),
            "script_writer": FormalCapabilityRoute(
                "script_writer", "脚本初稿", DEEPSEEK_PROVIDER,
                DEEPSEEK_MODEL, deepseek_ready,
            ),
            "script_editor": FormalCapabilityRoute(
                "script_editor", "脚本复核与编辑", DEEPSEEK_PROVIDER,
                DEEPSEEK_MODEL, deepseek_ready,
            ),
            "production_advisor": FormalCapabilityRoute(
                "production_advisor", "生产协作与问题诊断",
                DEEPSEEK_PROVIDER, DEEPSEEK_MODEL, deepseek_ready,
            ),
            "image_understanding": FormalCapabilityRoute(
                "image_understanding", "图片与扫描页理解",
                MINIMAX_VISION_PROVIDER, MINIMAX_VISION_MODEL, minimax_ready,
            ),
        }

    def resolve(self, capability: str) -> FormalCapabilityRoute:
        try:
            return self._routes[capability]
        except KeyError as exc:
            raise ValueError(f"未声明的 Formal 模型能力: {capability}") from exc


@dataclass(frozen=True)
class FormalRuntimeBundle:
    agent: ReadonlyAgentService | FormalExecutableAgentService
    brief_adapter: FormalBriefInputAdapter
    generation: FormalGenerationService
    script_workspace: FormalScriptWorkspaceService
    image_workspace: FormalImageWorkspaceService
    review_workspace: FormalReviewWorkspaceService
    admin: FormalAdminService
    recovery: FormalRecoveryExecutor


def uploaded_document(filename: str, stream: BinaryIO, critical: bool) -> UploadedDocument:
    return UploadedDocument(filename=filename, stream=stream, critical=critical)


def build_formal_source_root_resolver(
    runtime: dict, fallback_source_root: str | Path,
) -> Callable[[str], Path]:
    fallback = Path(fallback_source_root).expanduser().resolve()
    configured = dict(runtime.get("asset_packages") or {})

    def resolve(script_type: str) -> Path:
        normalized = normalize_script_type(script_type)
        category_id = category_id_for_script_type(normalized)
        package_path = _formal_package_path(runtime, normalized, category_id, configured)
        if not package_path:
            return fallback
        asset_package_source_record(package_path, expected_category_id=category_id)
        return Path(load_asset_package(package_path)["source_root_path"]).resolve()

    return resolve


def build_formal_runtime(
    config: dict | None = None,
    *,
    recovery_handlers: Mapping[str, RecoveryStageHandler] | None = None,
) -> FormalRuntimeBundle:
    """Compose one runtime authority and reconcile pending recovery events."""
    runtime = config or load_runtime_config()
    read_only_deployment = formal_agent_read_only(runtime)
    agent = ReadonlyAgentService.from_runtime_config(runtime)
    store = agent.services.store
    projects = agent.services.projects
    artifacts = agent.services.artifacts
    runs_root = Path(runtime["runs_root"])
    data_root = Path(runtime["data_root"])
    source_root = Path(runtime.get("source_root") or DEFAULT_SOURCE_ROOT).expanduser().resolve()
    source_root_resolver = build_formal_source_root_resolver(runtime, source_root)
    configured_packages = dict(runtime.get("asset_packages") or {})
    configured_by_category = {
        item.category_id: configured_packages.get(item.display_name) or configured_packages.get(item.category_id)
        or (runtime.get("asset_package") if item.category_id == "launch" else None)
        for item in SCRIPT_TYPE_METADATA
    }
    package_registry = LocalPackageRegistry(
        runtime.get("package_registry_path") or data_root / "package_registry.json",
        configured_packages=configured_by_category,
    )
    template_packages = AssetPackagePublicationService(
        storage=LocalAssetStorage(data_root / "assets"), registry=package_registry,
        packages_root=runtime.get("packages_root") or data_root / "packages",
    )

    def asset_package_resolver(script_type: str) -> dict | None:
        normalized = normalize_script_type(script_type)
        category_id = category_id_for_script_type(normalized)
        registry_path = str(runtime.get("package_registry_path") or "").strip()
        if registry_path and Path(registry_path).is_file():
            registry = LocalPackageRegistry(registry_path)
            if (registry.list()["aliases"].get(category_id) or {}).get("formal"):
                resolved = registry.resolve_formal(category_id)
                return {**resolved, "alias": "formal"}
        package_path = _formal_package_path(
            runtime, normalized, category_id, configured_packages
        )
        if not package_path:
            return None
        record = build_package_record(package_path)
        return {
            **record,
            "alias": "configured",
            "resolved_ref": f"{record['package_id']}@{record['package_version']}",
        }

    router = FormalCapabilityRouter()
    briefs = FormalBriefWorkspaceService(store, artifact_root=data_root)
    image_pool = ProjectImagePoolService(store, data_root=data_root)
    provider_policy = None
    vision_provider_policy = None
    understanding_policy = None
    ocr_policy = None
    text_route = router.resolve("brief_text_understanding")
    writer_route = router.resolve("script_writer")
    editor_route = router.resolve("script_editor")
    vision_route = router.resolve("image_understanding")
    advisor_route = router.resolve("production_advisor")
    advisor = None
    assistant_editor_policy = None
    if text_route.available and writer_route.available and editor_route.available:
        deepseek = build_text_provider(text_route.provider)

        def provider(prompt: str, model: str, max_tokens: int) -> str:
            route_limit = (
                DEEPSEEK_EDITOR_MAX_OUTPUT_TOKENS
                if "\nDRAFT:" in prompt
                else DEEPSEEK_WRITER_MAX_OUTPUT_TOKENS
            )
            return deepseek(
                prompt,
                model,
                min(max_tokens, route_limit),
            )

        provider_policy = FormalTextProviderPolicy(
            writer=provider,
            editor=provider,
            writer_provider=writer_route.provider,
            writer_model=writer_route.model,
            editor_provider=editor_route.provider,
            editor_model=editor_route.model,
        )
        assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=provider,
            provider=editor_route.provider,
            model=editor_route.model,
        )
        understanding_policy = FormalBriefUnderstandingPolicy(
            invoke=provider,
            provider=text_route.provider,
            model=text_route.model,
            max_output_tokens=DEEPSEEK_BRIEF_MAX_OUTPUT_TOKENS,
        )
        if advisor_route.available:
            advisor = FormalAdvisorService(FormalAdvisorPolicy(
                invoke=provider,
                provider=advisor_route.provider,
                model=advisor_route.model,
                max_output_tokens=DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS,
            ))
    if vision_route.available:
        vision_provider_policy = FormalVisionProviderPolicy(
            invoke=lambda path: generate_minimax_project_image_analysis(
                path, diagnostics_dir=data_root / "model-diagnostics" / "vision",
            ),
            provider=vision_route.provider,
            model=vision_route.model,
            prompt_version=PROJECT_IMAGE_VISION_PROMPT_VERSION,
        )
        ocr_policy = FormalBriefOcrPolicy(
            invoke=lambda path, prompt: _run_minimax_vision(
                "mmx", path, os.environ.get("MINIMAX_API_KEY", ""), 90, prompt=prompt,
            ),
            provider=vision_route.provider,
            model=vision_route.model,
            pdftoppm_command=os.environ.get("MPLUS_PDFTOPPM_COMMAND") or None,
        )
    adapter = FormalBriefInputAdapter(
        briefs,
        data_root=data_root,
        understanding_policy=understanding_policy,
        ocr_policy=ocr_policy,
    )
    brief_store = BriefArtifactStore(data_root)
    generation = FormalGenerationService(
        FormalGenerationServices(
            briefs=briefs,
            projects=projects,
            tasks=agent.services.tasks,
            artifacts=artifacts,
            images=image_pool,
        ),
        runs_root=runs_root,
        source_root=source_root,
        source_root_resolver=source_root_resolver,
        asset_package_resolver=asset_package_resolver,
        template_packages=template_packages,
        provider_policy=provider_policy,
        vision_provider_policy=vision_provider_policy,
        creative_stream_provider=(deepseek_http_stream_provider()
            if provider_policy is not None and writer_route.provider in {"deepseek", "deepseek-http"} else None),
        creative_writes_enabled=not read_only_deployment,
        brief_input_adapter=adapter,
    )
    if not read_only_deployment and os.environ.get("ZHIPU_API_KEY", "").strip():
        from .image_providers import zhipu_http_provider
        generation.image_provider = zhipu_http_provider()
    delivery = VersionDeliveryService(store, artifacts)
    quality = WorkspaceBoundVersionQuality(
        store=store,
        delivery=delivery,
        artifacts=artifacts,
        data_root=data_root,
    )
    commands = ManualReviewCommandService(ManualReviewCommandServices(
        store=store,
        delivery=delivery,
        quality=quality,
        final_confirmation=WorkspaceBoundFinalConfirmation(
            quality=quality,
            actor_resolver=WorkspaceReviewerActorResolver(store),
        ),
        evidence_authority=ReviewEvidenceAuthority(
            FileReviewEvidenceArtifactStore(data_root / "review-evidence"),
        ),
    ))
    image_workspace = FormalImageWorkspaceService(
        FormalImageWorkspaceServices(
            store=store,
            projects=projects,
            delivery=delivery,
            artifacts=artifacts,
            images=image_pool,
        ),
        runs_root=runs_root,
        source_root=source_root,
        source_root_resolver=source_root_resolver,
        package_registry=package_registry,
        semantic_policy=provider_policy,
    )
    script_workspace = FormalScriptWorkspaceService(
        FormalScriptWorkspaceServices(
            store=store,
            projects=projects,
            delivery=delivery,
            commands=commands,
            artifacts=artifacts,
            image_workspace=image_workspace,
        ),
        runs_root=runs_root,
        source_root=source_root,
        source_root_resolver=source_root_resolver,
        package_registry=package_registry,
    )
    script_revision = FormalScriptRevisionService(
        projects=projects,
        tasks=agent.services.tasks,
        workspace=script_workspace,
        writes_enabled=not read_only_deployment,
        image_workspace=image_workspace,
        brief_adapter=adapter,
        brief_store=brief_store,
        assistant_editor_policy=assistant_editor_policy,
    )
    generation.creative_revision = script_revision
    default_images = FormalDefaultImageService(
        projects=projects,
        tasks=agent.services.tasks,
        delivery=delivery,
        artifacts=artifacts,
        image_pool=image_pool,
        image_workspace=image_workspace,
        asset_package_resolver=asset_package_resolver,
        package_registry=package_registry,
        semantic_policy=provider_policy,
        creative_generation_service=generation,
        writes_enabled=not read_only_deployment,
    )
    script_revision.default_images = default_images
    review_workspace = FormalReviewWorkspaceService(
        FormalReviewWorkspaceServices(
            store=store,
            projects=projects,
            delivery=delivery,
            artifacts=artifacts,
            commands=commands,
            image_workspace=image_workspace,
        )
    )
    operations = FormalAdminService(store)

    def check_text_connection() -> bool:
        import json
        from .text_providers import deepseek_http_provider
        # A connection check is a single paid request, including on timeout.
        response = deepseek_http_provider(retry_delays=())(
            'Return only this JSON object: {"connection_check":"ok"}', text_route.model, 64,
        )
        return json.loads(response).get("connection_check") == "ok"

    def check_vision_connection() -> bool:
        import tempfile
        from PIL import Image
        from .image_understanding import _parse_minimax_response
        with tempfile.TemporaryDirectory(prefix="mplus-anonymous-vision-") as folder:
            path = Path(folder) / "red-square.png"
            Image.new("RGB", (128, 128), "red").save(path)
            response = _run_minimax_vision(
                "mmx", path, os.environ.get("MINIMAX_API_KEY", ""), 30,
                prompt='Identify the dominant color. Return only JSON: {"dominant_color":"red or another English color name"}',
            )
            parsed, diagnostics = _parse_minimax_response(response)
            return diagnostics["status"] == "parsed" and str(parsed.get("dominant_color", "")).strip().lower() == "red"

    operations.configure_connection_tests({
        "text": {"configured": text_route.available, "invoke": check_text_connection,
            "method": "固定匿名 JSON 文案样例", "real_provider": True},
        "vision": {"configured": vision_route.available, "invoke": check_vision_connection,
            "method": "固定匿名纯色图片识别", "real_provider": True},
    })
    handlers = {} if read_only_deployment else {"formal_generation_plan": generation.recover_confirmed_generation}
    handlers.update(recovery_handlers or {})
    recovery = operations.build_recovery_executor(
        handlers, authenticated_stages=frozenset({"formal_generation_plan"}),
    )
    recovery.consume_pending()
    formal_agent = FormalExecutableAgentService(
        ExecutableAgentServices(
            readonly=agent,
            projects=projects,
            tasks=agent.services.tasks,
            confirmations=ActionConfirmationService(
                store, load_or_create_confirmation_key(data_root),
            ),
            idempotency=ToolIdempotencyService(store),
            brief_store=brief_store,
            backend=FormalExecutionBackend(),
            delivery=delivery,
        ),
        generation=generation,
        advisor=advisor,
        writes_enabled=not read_only_deployment,
        read_only_deployment=read_only_deployment,
        script_revision=script_revision,
        default_images=default_images,
        operations=operations,
        review_workspace=review_workspace,
    )
    return FormalRuntimeBundle(
        agent=formal_agent,
        brief_adapter=adapter,
        generation=generation,
        script_workspace=script_workspace,
        image_workspace=image_workspace,
        review_workspace=review_workspace,
        admin=operations,
        recovery=recovery,
    )


def _formal_package_path(
    runtime: dict,
    normalized_script_type: str,
    category_id: str,
    configured: dict,
) -> str:
    registry_path = str(runtime.get("package_registry_path") or "").strip()
    if registry_path and Path(registry_path).is_file():
        try:
            registry = LocalPackageRegistry(registry_path)
            if (registry.list()["aliases"].get(category_id) or {}).get("formal"):
                return str(registry.resolve_formal(category_id)["local_path"])
        except (PackageRegistryError, OSError, ValueError) as exc:
            raise ValueError(f"Formal 资产包 alias 解析失败: {exc}") from exc
    return str(
        configured.get(normalized_script_type)
        or configured.get(category_id)
        or (runtime.get("asset_package") if normalized_script_type == "发布会" else "")
        or ""
    )
