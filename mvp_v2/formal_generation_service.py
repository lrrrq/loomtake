"""Formal Brief planning preview and confirmed script generation seam.

This service deliberately owns no schema, UI, agent registration, publication, or
CURRENT pointer.  It turns a T01 ``ConfirmedBriefInput`` into an auditable P00
preview, then requires the caller to submit that exact artifact reference and
SHA before the two-pass text route may run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import secrets
from typing import Callable, Mapping

from .artifact_service import ArtifactResolutionError, RunArtifactResolver
from .asset_package import load_asset_package
from .package_publication import AssetPackagePublicationService, PackagePublicationError
from .mutation_policy import require_active_project
from .formal_brief_domain import ArtifactPayload, ConfirmedBriefInput
from .creative_standard import PairedConfirmedBriefCreativeStandardInput
from . import creative_formal_binding
from .formal_brief_workspace_service import FormalBriefWorkspaceService
from .generation_stages import (
    _confirmed_brief_evidence,
    _planning_brief_from_confirmed_payload,
    read_script_draft,
    read_script_plan,
    run_script_plan_stage,
)
from .loop_runner import _generate, run_generation_once
from .image_understanding import ProjectImageVisionResult
from .identity_service import require_permission
from .knowledge_service import KnowledgeService
from .manual_version_commands import _audit as _manual_audit
from .manual_version_commands import _claim_idempotency, _sha256, _token
from .pipeline_runtime import finalize_pipeline_run, fork_pipeline_run, open_pipeline_run
from .pipeline_stages import (
    run_formal_context_media_stage,
    run_formal_context_visual_stage,
    run_input_freeze_stage,
    run_source_catalog_stage,
)
from .project_service import ProjectService
from .project_image_authority import (
    ManualImageAnalysisInput, ManualProjectImageAnalysisCommandService,
    ProjectImagePlanningReadiness, ProjectImagePoolService,
    ProviderImageAnalysisInput, ProviderProjectImageAnalysisCommandService,
)
from .run_contract import sha256_file, sha256_json
from .package_registry import build_package_record, PackageRegistryError
from .script_draft import MAX_OUTPUT_TOKENS, TextProvider
from .script_type_adapter import category_id_for_script_type
from .task_service import TaskService
from .workspace_store import WorkspaceServiceError
from .workspace_store import new_id, utc_now
from .version_review_repository import require_no_unsaved_script_draft
from .run_state import write_json_atomic


class FormalGenerationBlocked(WorkspaceServiceError):
    """Stable, user-visible stop for the formal generation journey."""


@dataclass(frozen=True)
class FormalTextProviderPolicy:
    """Server-owned route only; no UI/API model or token-budget override exists."""

    writer: TextProvider
    editor: TextProvider
    writer_provider: str
    writer_model: str
    editor_provider: str
    editor_model: str


@dataclass(frozen=True)
class FormalVisionProviderPolicy:
    """Server-owned vision route; browsers cannot select provider or model."""

    invoke: Callable[[Path], ProjectImageVisionResult]
    provider: str
    model: str
    prompt_version: str
    max_batch_size: int = 8

    def __post_init__(self) -> None:
        if type(self.max_batch_size) is not int or not 1 <= self.max_batch_size <= 20:
            raise ValueError("Formal 图片自动理解批量必须在 1..20")


@dataclass(frozen=True)
class FormalGenerationServices:
    briefs: FormalBriefWorkspaceService
    projects: ProjectService
    tasks: TaskService
    artifacts: RunArtifactResolver
    images: ProjectImagePoolService | None = None
    knowledge: KnowledgeService | None = None


class FormalGenerationService:
    """R1 preview and R2 generation without a fallback to raw briefs or drafts."""

    def __init__(
        self,
        services: FormalGenerationServices,
        *,
        runs_root: str | Path,
        source_root: str | Path,
        source_root_resolver: Callable[[str], Path] | None = None,
        asset_package_resolver: Callable[[str], Path | Mapping | None] | None = None,
        template_packages: AssetPackagePublicationService | None = None,
        provider_policy: FormalTextProviderPolicy | None = None,
        vision_provider_policy: FormalVisionProviderPolicy | None = None,
        creative_stream_provider=None,
        creative_writes_enabled: bool = True,
        brief_input_adapter=None,
    ):
        self.services = services
        self.runs_root = Path(runs_root).expanduser().resolve()
        self.source_root = Path(source_root).expanduser().resolve()
        self.source_root_resolver = source_root_resolver
        self.asset_package_resolver = asset_package_resolver
        self.template_packages = template_packages
        self._legacy_asset_package_cache: dict[Path, dict] = {}
        self.knowledge = services.knowledge or KnowledgeService(services.projects.store)
        self.provider_policy = provider_policy
        self.vision_provider_policy = vision_provider_policy
        self.creative_stream_provider = creative_stream_provider
        self.creative_writes_enabled = creative_writes_enabled
        self.brief_input_adapter = brief_input_adapter
        self.creative_revision = None
        self.image_provider = None

    def inherit_creative_images(self, auth_token: str, *, workspace_id: str, project_id: str, version_id: str) -> dict:
        from .creative_formal_binding import inherit_images
        self._template_project(auth_token, workspace_id, project_id, write=True)
        return inherit_images(self, auth_token, workspace_id=workspace_id, project_id=project_id, version_id=version_id)

    def image_creation_context(self, auth_token: str, **kwargs) -> dict:
        from .creative_images import context
        return context(self, auth_token, **kwargs)

    def prepare_image_prompt(self, auth_token: str, **kwargs) -> dict:
        from .creative_images import prepare
        return prepare(self, auth_token, **kwargs)

    def start_image_creation(self, auth_token: str, **kwargs) -> dict:
        from .creative_images import start
        return start(self, auth_token, **kwargs)

    def execute_image_creation(self, auth_token: str, **kwargs) -> None:
        from .creative_images import execute
        execute(self, auth_token, **kwargs)

    def read_image_creation(self, auth_token: str, **kwargs) -> dict:
        from .creative_images import read
        return read(self, auth_token, **kwargs)

    def preview_image_creation(self, auth_token: str, **kwargs) -> tuple[bytes, str]:
        from .creative_images import preview
        return preview(self, auth_token, **kwargs)

    def adopt_image_creation(self, auth_token: str, **kwargs) -> dict:
        from .creative_images import adopt
        return adopt(self, auth_token, **kwargs)

    def save_creative_image_references(self, auth_token: str, **kwargs) -> dict:
        from .creative_images import save_references
        return save_references(self, auth_token, **kwargs)

    def preview_creative_image_reference(self, auth_token: str, **kwargs):
        from .creative_images import reference_preview
        return reference_preview(self, auth_token, **kwargs)

    def read_creative_draft(self, auth_token: str, *, workspace_id: str, project_id: str,
                            since_task_id: str | None = None, after_sequence: int = 0,
                            known_revision: int | None = None, known_brief_marker: str | None = None) -> dict:
        from .creative_generation import read
        return read(self, auth_token, workspace_id=workspace_id, project_id=project_id,
                    since_task_id=since_task_id, after_sequence=after_sequence,
                    known_revision=known_revision, known_brief_marker=known_brief_marker)

    def start_creative_draft(self, auth_token: str, **kwargs) -> dict:
        from .creative_generation import start
        return start(self, auth_token, **kwargs)

    def execute_creative_draft(self, auth_token: str, **kwargs) -> None:
        from .creative_generation import execute
        execute(self, auth_token, **kwargs)

    def _template_service(self) -> AssetPackagePublicationService:
        if self.template_packages is None:
            raise FormalGenerationBlocked("TEMPLATE_UNAVAILABLE", "模板库尚未接入")
        return self.template_packages

    def _template_project(self, auth_token: str, workspace_id: str, project_id: str,
                          expected: int | None = None, *, write: bool = False) -> dict:
        project = self.services.projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise FormalGenerationBlocked("CROSS_PROJECT_DENIED", "项目不属于当前工作区")
        if expected is not None and project["project_version"] != expected:
            raise FormalGenerationBlocked("PROJECT_VERSION_CONFLICT", "项目已变化，请刷新后重试")
        if write:
            require_active_project(project)
            with self.services.projects.store.connection() as connection:
                require_permission(connection, auth_token, workspace_id=workspace_id,
                    project_id=project_id, permission="project.update")
        return project

    def template_catalog(self, auth_token: str, *, workspace_id: str, project_id: str,
                         target_category_id: str | None = None) -> dict:
        project = self._template_project(auth_token, workspace_id, project_id)
        service = self._template_service()
        category = target_category_id or project["category_id"]
        from .script_type_adapter import SCRIPT_TYPE_METADATA, metadata_for_category_id
        if category:
            try:
                metadata_for_category_id(category)
            except ValueError as exc:
                raise WorkspaceServiceError('INVALID_INPUT', '请选择受支持的项目类型') from exc
        switching = category != project['category_id']
        if category is None:
            return {"project_id": project_id, "project_version": project["project_version"],
                    "category_id": None, "selected_ref": "", "is_bound": False,
                    "items": [], "can_trial": False, "notice": "确认 Brief 类型后匹配模板。",
                    "categories": [{'category_id': m.category_id, 'label': m.display_name} for m in SCRIPT_TYPE_METADATA],
                    "current_category_id": project['category_id'], "requires_brief_confirmation": False}
        registry = service.registry.list()
        default_ref = str((registry["aliases"].get(category) or {}).get("formal") or "")
        if not default_ref:
            configured = service.registry.configured_record(category)
            default_ref = str(configured["resolved_ref"]) if configured else ""
        selected_ref = str((project.get("template_package_ref") if not switching else '') or default_ref)
        latest = {}
        for record in service.template_records(category):
            key = record["package_id"]
            if key not in latest or record["package_version"] > latest[key]["package_version"]:
                latest[key] = record
        refs = {f"{item['package_id']}@{item['package_version']}" for item in latest.values()}
        refs.update(ref for ref in (selected_ref, default_ref) if ref)
        items = []
        for ref in sorted(refs):
            try:
                item = service.describe_template(service.resolve_template(category, ref))
            except (PackagePublicationError, ValueError, OSError):
                continue
            items.append({**item, "is_default": ref == default_ref, "selected": ref == selected_ref})
        can_trial = False
        try:
            self._resolve_current_paired(auth_token, workspace_id, project_id)
            can_trial = project["status"] == "active" and not switching
        except WorkspaceServiceError:
            pass
        return {"project_id": project_id, "project_version": project["project_version"],
            "category_id": category, "current_category_id": project['category_id'],
            "requires_brief_confirmation": switching,
            "categories": [{'category_id': m.category_id, 'label': m.display_name} for m in SCRIPT_TYPE_METADATA],
            "selected_ref": selected_ref, "is_bound": bool(project.get("template_package_ref")) and not switching, "items": items,
            "can_trial": can_trial, "notice": "先确认目标类型的需求，再选择模板并预览新规划。" if switching else "模板已固定，后续规划使用此版本。" if project.get("template_package_ref")
            else "当前使用类别默认模板。" if items else "暂无可用模板，请导入已就绪模板。"}

    def import_template(self, auth_token: str, *, workspace_id: str, project_id: str,
                        expected_project_version: int, filename: str, content: bytes,
                        display_name: str = "") -> dict:
        project = self._template_project(auth_token, workspace_id, project_id, expected_project_version, write=True)
        service = self._template_service()
        try:
            record = service.import_template(category_id=project["category_id"], filename=filename,
                content=content, display_name=display_name)
            preview = service.describe_template(record)
        except PackagePublicationError as exc:
            raise FormalGenerationBlocked(exc.code, str(exc).split(": ", 1)[-1]) from exc
        return {**self.template_catalog(auth_token, workspace_id=workspace_id, project_id=project_id),
                "preview": preview, "notice": "导入完成，请检查结构后采用。"}

    def select_template(self, auth_token: str, *, workspace_id: str, project_id: str,
                        expected_project_version: int, template_ref: str, confirmed: bool) -> dict:
        project = self._template_project(auth_token, workspace_id, project_id, expected_project_version, write=True)
        if not confirmed:
            raise FormalGenerationBlocked("CONFIRMATION_REQUIRED", "请确认采用此模板")
        try:
            record = self._template_service().resolve_template(project["category_id"], template_ref)
        except PackagePublicationError as exc:
            raise FormalGenerationBlocked(exc.code, str(exc).split(": ", 1)[-1]) from exc
        if record["resolved_ref"] not in self._template_service().registry.list()["packages"]:
            self._template_service().registry.register_records([record])
        self.services.projects.select_template(auth_token, project_id=project_id,
            package_ref=record["resolved_ref"], package_sha256=record["package_sha256"],
            expected_project_version=expected_project_version)
        return {**self.template_catalog(auth_token, workspace_id=workspace_id, project_id=project_id),
                "notice": "已采用。下次规划生效，已保存脚本保持原模板。"}

    def trial_template(self, auth_token: str, *, workspace_id: str, project_id: str,
                       expected_project_version: int, template_ref: str,
                       generation_mode: str = "text_only_draft") -> dict:
        project = self._template_project(auth_token, workspace_id, project_id, expected_project_version, write=True)
        try:
            record = self._template_service().resolve_template(project["category_id"], template_ref)
            item = self._template_service().describe_template(record)
        except PackagePublicationError as exc:
            raise FormalGenerationBlocked(exc.code, str(exc).split(": ", 1)[-1]) from exc
        plan = self.prepare_plan(auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=expected_project_version, generation_mode=generation_mode,
            template_ref=template_ref, trial_only=True)
        return {"template": item, "plan": plan}

    def _script_replan_intent(
        self,
        auth_token: str,
        *,
        project_id: str,
        revision_task_id: str | None,
    ) -> dict | None:
        task_id = str(revision_task_id or "").strip()
        if not task_id:
            return None
        task = self.services.tasks.get_task(auth_token, task_id)
        if (
            task["project_id"] != project_id
            or task["task_type"] != "formal_script_revision"
            or task["status"] != "needs_clarification"
        ):
            raise FormalGenerationBlocked(
                "SCRIPT_REPLAN_TASK_INVALID", "脚本重规划任务不属于当前项目或状态已变化",
            )
        event = next((
            item
            for item in reversed(self.services.tasks.list_task_events(auth_token, task_id))
            if isinstance(item.get("details", {}).get("internal_replan_intent"), dict)
        ), None)
        if event is None:
            raise FormalGenerationBlocked(
                "SCRIPT_REPLAN_TASK_INVALID", "脚本重规划任务缺少服务端意图证据",
            )
        raw = event["details"]["internal_replan_intent"]
        before = raw.get("shot_count_before")
        after = raw.get("shot_count_after")
        protected = raw.get("protected_shots") or []
        if (
            type(before) is not int or before <= 0
            or type(after) is not int or after <= 0 or after == before
            or not isinstance(protected, list)
            or any(not isinstance(item, dict) or not item.get("shot_id") for item in protected)
            or len(protected) > after
        ):
            raise FormalGenerationBlocked(
                "SCRIPT_REPLAN_TARGET_REQUIRED", "请明确重规划后的总镜数，并确保可容纳保护镜头",
            )
        return {
            "revision_task_id": task_id,
            "shot_count_before": before,
            "shot_count_after": after,
            "protected_shots": protected,
        }

    def prepare_plan(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int,
        template_conflict_resolutions: dict[str, str] | None = None,
        conflict_resolution_binding: dict | None = None,
        generation_mode: str | None = None,
        operation_key: str | None = None,
        revision_task_id: str | None = None,
        template_ref: str | None = None,
        trial_only: bool = False,
        expected_creative_revision: int | None = None,
    ) -> dict:
        """Run P00 only and persist an explicit user-confirmation checkpoint."""
        operation_key = str(operation_key or "").strip() or None
        project = self._project_at_lock(
            auth_token, workspace_id, project_id, expected_project_version
        )
        self._require_no_unsaved_script_draft(project)
        replan_intent = self._script_replan_intent(
            auth_token, project_id=project_id, revision_task_id=revision_task_id,
        )
        paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
        creative_binding = (
            creative_formal_binding.freeze(self, auth_token, workspace_id=workspace_id,
                project_id=project_id, expected_revision=expected_creative_revision)
            if expected_creative_revision is not None else None
        )
        if creative_binding and (revision_task_id or project.get("current_version_id")):
            raise FormalGenerationBlocked("INVALID_INPUT", "初稿转换只用于首个正式版本；已有版本请在当前脚本继续修改")
        confirmed_brief = paired.brief
        task = self._new_task(
            auth_token, project_id, expected_project_version, paired,
            operation_key=operation_key,
        )
        cache_root = self._project_cache(project_id)
        run_id = ""
        semantic_calls = 0
        try:
            planning_brief = self._planning_brief(
                auth_token, workspace_id, project_id, confirmed_brief
            )
            asset_package, package_evidence = self._asset_package_for(
                planning_brief["脚本类型"], project=project, template_ref=template_ref
            )
            source_root = self._source_root_for(
                planning_brief["脚本类型"], asset_package=asset_package,
            )
            effective_knowledge = self._effective_knowledge(auth_token, project_id)
            rule_contract = (
                load_asset_package(asset_package)["template_rule_contract"]
                if asset_package is not None else None
            )
            task = self.services.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=task["task_version"],
                status="executing",
                details={
                    "action": "prepare_plan", "provider_calls": 0,
                    **(
                        {"operation_key": operation_key, "outer_task_id": operation_key}
                        if operation_key else {}
                    ),
                    "project_id": project_id,
                    "expected_project_version": expected_project_version,
                },
            )
            paired_content = self.services.briefs.creative_standard_resolver().resolve(
                paired.creative_standard
            )
            images = self._synchronize_and_resolve_images(
                auth_token, workspace_id=workspace_id, project_id=project_id,
                paired=paired, expected_project_version=expected_project_version,
            )
            if generation_mode is None:
                raise FormalGenerationBlocked(
                    "IMAGE_STRATEGY_CONFIRMATION_REQUIRED",
                    "请明确选择上传图片、采用类别图片或只生成文字草稿",
                )
            effective_mode = generation_mode
            if effective_mode not in {"image_backed", "text_only_draft"}:
                raise FormalGenerationBlocked("INVALID_INPUT", "图片策略无效")
            if images.total == 0 and effective_mode != "text_only_draft":
                raise FormalGenerationBlocked(
                    "IMAGE_STRATEGY_CONFIRMATION_REQUIRED", "图片模式需要先上传或采用类别图片",
                )
            self._require_images_ready(images, generation_mode=effective_mode)
            image_context = self._image_context_readiness(images)
            image_evidence = self._image_event_evidence(images)
            with open_pipeline_run(
                planning_brief,
                cache_root,
                source_root,
                cache_fingerprint=sha256_json({
                    "confirmed_brief": _confirmed_brief_evidence(confirmed_brief),
                    "confirmed_creative_standard": paired.creative_standard.as_mapping(),
                    "project_image_context": image_evidence,
                    "source_view": "ai-spec",
                    "template_rules_sha256": (
                        str(rule_contract.get("sha256") or "") if rule_contract else ""
                    ),
                    "template_conflict_resolutions": template_conflict_resolutions or {},
                    "conflict_resolution_binding": conflict_resolution_binding or {},
                    "formal_asset_package": package_evidence,
                    "effective_knowledge": effective_knowledge,
                    **({"creative_draft_binding": creative_binding} if creative_binding else {}),
                    **(
                        {"script_replan_intent": replan_intent}
                        if replan_intent else {}
                    ),
                }),
                category_id=category_id_for_script_type(planning_brief["脚本类型"]),
                provenance={
                    "formal_asset_package": package_evidence,
                    "effective_knowledge": effective_knowledge,
                },
            ) as state:
                run_id = state.run_id
                frozen = run_input_freeze_stage(
                    state,
                    brief=planning_brief,
                    config={
                        "source_view": "ai-spec",
                        "formal_generation": "R1",
                        "effective_knowledge": effective_knowledge,
                    },
                    source_root=source_root,
                    brief_ref=confirmed_brief.payload.ref,
                    asset_package=asset_package,
                    asset_package_evidence=package_evidence,
                )
                run_source_catalog_stage(
                    state,
                    input_freeze_path=frozen,
                    brief=planning_brief,
                    source_root=source_root,
                    source_view="ai-spec",
                    asset_package=asset_package,
                    asset_package_evidence=package_evidence,
                    library_packages=self._library_paths(planning_brief["脚本类型"], package_evidence),
                )
                media = run_formal_context_media_stage(
                    state, input_freeze_path=frozen, readiness=image_context,
                )
                run_formal_context_visual_stage(state, media_manifest=media)
                plan = run_script_plan_stage(
                    state,
                    briefs=[],
                    constraints={
                        **({"formal_project_context": planning_brief["_formal_project_context"]}
                           if "_formal_project_context" in planning_brief else {}),
                        "project_image_context": image_evidence,
                        "effective_knowledge": effective_knowledge,
                        "formal_asset_package": package_evidence,
                        **({"creative_draft_binding": creative_binding} if creative_binding else {}),
                        **(
                            {
                                "planned_shot_count": replan_intent["shot_count_after"],
                                "script_replan_intent": replan_intent,
                            }
                            if replan_intent else {}
                        ),
                        **({"template_rule_contract": rule_contract} if rule_contract else {}),
                        **(
                            {"template_conflict_resolutions": template_conflict_resolutions}
                            if template_conflict_resolutions else {}
                        ),
                        **(
                            {"conflict_resolution_binding": conflict_resolution_binding}
                            if conflict_resolution_binding else {}
                        ),
                    },
                    source_view="ai-spec",
                    builder=lambda resolved: _generate(
                        resolved,
                        source_root,
                        {
                            "creative_standard": paired_content,
                            "project_image_context": image_evidence,
                            "effective_knowledge": effective_knowledge,
                            "formal_asset_package": package_evidence,
                            **({"creative_draft_binding": creative_binding} if creative_binding else {}),
                            **(
                                {
                                    "planned_shot_count": replan_intent["shot_count_after"],
                                    "script_replan_intent": replan_intent,
                                }
                                if replan_intent else {}
                            ),
                            **({"template_rule_contract": rule_contract} if rule_contract else {}),
                            **(
                                {"template_conflict_resolutions": template_conflict_resolutions}
                                if template_conflict_resolutions else {}
                            ),
                            **(
                                {"conflict_resolution_binding": conflict_resolution_binding}
                                if conflict_resolution_binding else {}
                            ),
                        },
                        None,
                        "ai-spec",
                        asset_package=asset_package,
                        library_packages=self._library_paths(planning_brief["脚本类型"], package_evidence),
                        semantic_policy=self.provider_policy if not trial_only else None,
                        semantic_evidence_path=state.run_dir / 'template_semantic_matching.json',
                    ),
                    confirmed_brief_input=confirmed_brief,
                    confirmed_brief_resolver=self.services.briefs.resolver(),
                    confirmed_creative_standard_input=paired.creative_standard,
                    confirmed_creative_standard_resolver=self.services.briefs.creative_standard_resolver(),
                )
                self._require_plan_display_complete(plan)
                semantic_payload = json.loads(Path(plan.path).read_text(encoding='utf-8'))
                semantic_calls = sum(semantic_payload.get(key, {}).get('provider_calls', 0)
                    for key in ('semantic_matching', 'planning_semantics'))
                finalize_pipeline_run(
                    state,
                    "waiting_user",
                    {"plan": plan.path},
                    metrics={
                        "provider_calls": semantic_calls,
                        "formal_asset_package": package_evidence,
                        "effective_knowledge_snapshot_sha256": effective_knowledge[
                            "snapshot_sha256"
                        ],
                    },
                )
            manifest = self.services.artifacts.resolve_manifest(run_id)
            plan_artifact = self.services.artifacts.resolve_stage_artifact(
                run_id, "script_plan", expected_manifest_sha256=manifest.sha256
            )
        except Exception as exc:
            blocked = self._as_blocked(exc)
            if run_id:
                try:
                    semantic_calls = sum(json.loads(path.read_text(encoding='utf-8')).get('provider_calls', 0)
                        for path in (state.run_dir / 'template_semantic_matching.json', state.run_dir / 'planning_semantics.json')
                        if path.is_file())
                    finalize_pipeline_run(
                        state,
                        "blocked",
                        {},
                        metrics={"provider_calls": semantic_calls, "block_code": blocked.code},
                    )
                except Exception:
                    pass
            self._block_task(auth_token, task, blocked.code, {"action": "prepare_plan", "provider_calls": semantic_calls})
            raise blocked from exc

        self._require_same_current_pair(auth_token, workspace_id, project_id, paired)
        self._require_same_images(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            paired=paired, expected_project_version=expected_project_version,
            expected=image_evidence, generation_mode=effective_mode,
        )
        self._require_same_generation_inputs(
            auth_token,
            project_id=project_id,
            script_type=planning_brief["脚本类型"],
            expected_package=package_evidence,
            expected_knowledge=effective_knowledge,
            template_ref=template_ref,
        )

        creative_formal_binding.require_current(self, auth_token, workspace_id=workspace_id,
            project_id=project_id, binding=creative_binding)
        bound = project if trial_only else self.services.projects.bind_run(
            auth_token,
            project_id=project_id,
            run_id=run_id,
            source_kind="native",
            manifest_ref=manifest.ref,
            manifest_sha256=manifest.sha256,
            expected_project_version=expected_project_version,
        )
        task = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="completed" if trial_only else "waiting_user",
            result_ref=plan_artifact.ref,
            details={
                "action": "trial_template" if trial_only else "prepare_plan",
                "plan_ref": plan_artifact.ref,
                "plan_sha256": plan_artifact.sha256,
                "manifest_ref": manifest.ref,
                "manifest_sha256": manifest.sha256,
                "provider_calls": 0,
                "workspace_id": confirmed_brief.workspace_id,
                "project_id": confirmed_brief.project_id,
                "brief_version_id": confirmed_brief.brief_version_id,
                "confirmation_id": confirmed_brief.confirmation_id,
                "brief_snapshot_id": confirmed_brief.brief_snapshot_id,
                "brief_payload_ref": confirmed_brief.payload.ref,
                "brief_payload_sha256": confirmed_brief.payload.sha256,
                "project_image_context": image_evidence,
                "generation_mode": effective_mode,
                "formal_asset_package": package_evidence,
                "effective_knowledge": effective_knowledge,
                "script_replan_intent": replan_intent,
                **({"creative_draft_binding": creative_binding} if creative_binding else {}),
                **(
                    {"operation_key": operation_key, "outer_task_id": operation_key}
                    if operation_key else {}
                ),
                "expected_project_version": expected_project_version,
                **self._paired_event_evidence(paired),
            },
        )
        dto = self._plan_dto(task, plan, plan_artifact, manifest, bound)
        if trial_only:
            dto["status"] = "trial_completed"
        return dto

    def get_plan_preview(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        plan_id: str,
        expected_project_version: int | None = None,
    ) -> dict:
        """Rebuild a waiting formal-plan DTO from immutable task evidence only."""
        task = self._require_plan_task(auth_token, project_id, plan_id)
        before = self._current_plan_state(
            auth_token, workspace_id, project_id, expected_project_version
        )
        if task["status"] != "waiting_user":
            raise FormalGenerationBlocked(
                "PLAN_NOT_WAITING_CONFIRMATION", "规划不处于待确认状态"
            )
        event = self._plan_event(auth_token, plan_id)
        paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
        manifest, artifact, raw = self._require_current_plan_pair(auth_token, event, paired)
        if self._current_plan_state(auth_token, workspace_id, project_id, None) != before:
            raise FormalGenerationBlocked(
                "PROJECT_STATE_CHANGED", "读取规划期间项目锁或当前 Brief 已变化"
            )
        planning_brief = self._planning_brief(
            auth_token, workspace_id, project_id, paired.brief,
        )
        _, package_evidence = self._asset_package_for(planning_brief["脚本类型"],
            project=self.services.projects.get_project(auth_token, project_id),
            frozen_evidence=event.get("formal_asset_package"))
        effective_knowledge = self._effective_knowledge(auth_token, project_id)
        images = self._resolve_images(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            paired=paired, expected_project_version=before["project_version"],
        )
        generation_mode = str(event.get("generation_mode") or "")
        image_evidence = self._image_event_evidence(images)
        self._validate_plan_event(
            event,
            paired,
            image_evidence,
            package_evidence,
            effective_knowledge,
        )
        self._require_images_ready(images, generation_mode=generation_mode)
        try:
            plan = read_script_plan(artifact.path)
            if sha256_file(artifact.path) != artifact.sha256:
                raise FormalGenerationBlocked(
                    "PLAN_ARTIFACT_DRIFT", "规划 artifact 在读取期间已变化"
                )
        except (OSError, TypeError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise FormalGenerationBlocked(
                "PLAN_ARTIFACT_UNAVAILABLE", "规划 artifact 无法读取"
            ) from exc
        evidence = _confirmed_brief_evidence(paired.brief)
        standard = paired.creative_standard
        if (
            plan.confirmed_brief_input != evidence
            or raw.get("confirmed_brief_provenance", {}).get("brief_version_id")
            != paired.brief.brief_version_id
            or raw.get("confirmed_brief_provenance", {}).get("payload_sha256")
            != paired.brief.payload.sha256
            or raw.get("confirmed_creative_standard_provenance", {}) != {
                "creative_standard_version_id": standard.creative_standard_version_id,
                "confirmation_id": standard.confirmation_id,
                "brief_version_id": standard.brief_version_id,
                "brief_snapshot_id": standard.brief_snapshot_id,
                "brief_payload": standard.brief_payload.as_mapping(),
                "card_payload": standard.card_payload.as_mapping(),
            }
            or raw.get("constraints", {}).get("project_image_context") != image_evidence
            or raw.get("constraints", {}).get("formal_asset_package")
            != package_evidence
            or raw.get("constraints", {}).get("effective_knowledge", {}).get(
                "snapshot_sha256"
            )
            != effective_knowledge["snapshot_sha256"]
        ):
            raise FormalGenerationBlocked(
                "PLAN_EVENT_INVALID", "规划 artifact 与当前正式 Brief 绑定不一致"
            )
        self._require_plan_display_complete(plan)
        creative_binding = plan.constraints.get("creative_draft_binding")
        if creative_binding != event.get("creative_draft_binding"):
            raise FormalGenerationBlocked("PLAN_EVENT_INVALID", "初稿绑定与规划证据不一致")
        creative_formal_binding.require_current(self, auth_token, workspace_id=workspace_id,
            project_id=project_id, binding=creative_binding)
        after = self._current_plan_state(
            auth_token, workspace_id, project_id, expected_project_version
        )
        if after != before:
            raise FormalGenerationBlocked(
                "PROJECT_STATE_CHANGED", "读取规划期间项目锁或当前 Brief 已变化"
            )
        self._require_same_current_pair(auth_token, workspace_id, project_id, paired)
        self._require_same_images(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            paired=paired, expected_project_version=before["project_version"],
            expected=image_evidence, generation_mode=generation_mode,
        )
        self._require_same_generation_inputs(
            auth_token,
            project_id=project_id,
            script_type=planning_brief["脚本类型"],
            expected_package=package_evidence,
            expected_knowledge=effective_knowledge,
        )
        return self._plan_dto(task, plan, artifact, manifest, {"project_version": before["project_version"]})

    def prepare_conflict_resolutions(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        plan_id: str,
        plan_sha256: str,
        expected_project_version: int,
        idempotency_key: str,
        resolutions: list[dict],
    ) -> dict:
        authoritative = self._authoritative_conflict_resolutions(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            plan_id=plan_id,
            plan_sha256=plan_sha256,
            expected_project_version=expected_project_version,
            resolutions=resolutions,
        )
        tool_name = "resolve_plan_template_conflicts"
        normalized = {
            "project_id": project_id,
            "plan_id": plan_id,
            "plan_sha256": plan_sha256,
            "expected_project_version": expected_project_version,
            "idempotency_key": str(idempotency_key or "").strip(),
            "resolutions": authoritative["resolutions"],
        }
        if not normalized["idempotency_key"]:
            raise WorkspaceServiceError("INVALID_INPUT", "idempotency_key 不能为空")
        digest = _sha256({
            "tool_name": tool_name,
            "parameters": normalized,
            "binding": authoritative["binding"],
        })
        command_id, nonce = new_id("manual_command"), secrets.token_urlsafe(24)
        store = self.services.projects.store
        with store.transaction() as connection:
            project = connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            actor = require_permission(
                connection, auth_token, workspace_id=workspace_id,
                project_id=project_id, permission="project.update",
            )
            if project is None or int(project["lock_version"]) != expected_project_version:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目版本已变化，请刷新后重试")
            context = {"actor_id": actor["actor_id"], "project_id": project_id}
            connection.execute(
                "INSERT INTO manual_command_confirmations VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)",
                (command_id, actor["actor_id"], project_id, tool_name, digest, _sha256(nonce), utc_now()),
            )
            _claim_idempotency(
                connection, context, tool_name, normalized["idempotency_key"], digest, command_id,
            )
            _manual_audit(connection, command_id, context, tool_name, "prepared", digest)
        return {
            "command_id": command_id,
            "command_token": f"{command_id}.{nonce}",
            "parameters_sha256": digest,
            "risk": "R2",
            "channel": "manual_page",
            "resolutions": authoritative["resolutions"],
            "will_change": (
                "保存不可变冲突决定并立即重新规划；选择模板优先时会创建并确认派生 Brief。"
            ),
            "will_not_change": (
                "不会覆盖原 Brief、原 Plan、历史 run 或脚本版本；不会调用写稿、编辑或智能体模型。"
            ),
            "model_calls": 0,
            "confirmation_label": "我已核对全部冲突并确认按所选方案重新规划",
        }

    def execute_conflict_resolutions(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        plan_id: str,
        plan_sha256: str,
        expected_project_version: int,
        idempotency_key: str,
        resolutions: list[dict],
        command_token: str,
    ) -> dict:
        authoritative = self._authoritative_conflict_resolutions(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            plan_id=plan_id,
            plan_sha256=plan_sha256,
            expected_project_version=expected_project_version,
            resolutions=resolutions,
        )
        normalized = {
            "project_id": project_id, "plan_id": plan_id,
            "plan_sha256": plan_sha256,
            "expected_project_version": expected_project_version,
            "idempotency_key": str(idempotency_key or "").strip(),
            "resolutions": authoritative["resolutions"],
        }
        tool_name = "resolve_plan_template_conflicts"
        digest = _sha256({
            "tool_name": tool_name,
            "parameters": normalized,
            "binding": authoritative["binding"],
        })
        command_id, nonce = _token(command_token)
        with self.services.projects.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM manual_command_confirmations WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None or row["status"] != "pending" or (
                row["project_id"], row["tool_name"], row["parameters_sha256"], row["nonce_hash"]
            ) != (project_id, tool_name, digest, _sha256(nonce)):
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED", "冲突解决确认无效、已消费或参数已变化"
                )
        resolution_payload = {
            "schema_version": "mplus-plan-conflict-resolution/v1",
            "workspace_id": workspace_id,
            "project_id": project_id,
            "source_plan_id": plan_id,
            "source_plan_sha256": plan_sha256,
            "brief_sha256": authoritative["binding"]["brief_sha256"],
            "template_rules_sha256": authoritative["binding"]["template_rules_sha256"],
            "confirmation_id": command_id,
            "resolutions": authoritative["resolutions"],
            "model_calls": 0,
            "created_at": utc_now(),
        }
        resolution_artifact = self.services.briefs.artifacts.write(
            json.dumps(
                resolution_payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        current_version = expected_project_version
        derived_brief_version_id = ""
        template_removals = [
            item for item in authoritative["resolutions"]
            if item["choice"] == "template_wins"
        ]
        if template_removals:
            paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
            derived = self.services.briefs.derive_template_resolution(
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                parent_brief_version_id=paired.brief.brief_version_id,
                removals=template_removals,
                resolution_artifact=resolution_artifact,
                expected_project_version=current_version,
            )
            current_version += 1
            self.services.briefs.confirm(
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                brief_version_id=derived.brief_version_id,
                confirmed_at=utc_now(),
                expected_project_version=current_version,
            )
            current_version += 1
            derived_brief_version_id = derived.brief_version_id
        choices = {
            item["conflict_id"]: item["choice"]
            for item in authoritative["resolutions"]
        }
        source_plan_event = self._plan_event(auth_token, plan_id)
        source_replan_intent = dict(source_plan_event.get("script_replan_intent") or {})
        source_creative_binding = source_plan_event.get("creative_draft_binding")
        creative_formal_binding.require_current(self, auth_token, workspace_id=workspace_id,
            project_id=project_id, binding=source_creative_binding)
        new_plan = self.prepare_plan(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=current_version,
            template_conflict_resolutions=choices,
            conflict_resolution_binding={
                "artifact": resolution_artifact.as_mapping(),
                "source_plan_id": plan_id,
                "source_plan_sha256": plan_sha256,
                "confirmation_id": command_id,
                "resolutions": authoritative["resolutions"],
            },
            generation_mode=str(source_plan_event.get("generation_mode") or "image_backed"),
            revision_task_id=(
                str(source_replan_intent.get("revision_task_id") or "") or None
            ),
            expected_creative_revision=(
                creative_formal_binding.repository(self, auth_token, workspace_id, project_id).read()["revision"]
                if source_creative_binding else None
            ),
        )
        source_task = self._require_plan_task(auth_token, project_id, plan_id)
        if source_task["status"] == "waiting_user":
            self.services.tasks.transition_task(
                auth_token,
                task_id=plan_id,
                expected_task_version=source_task["task_version"],
                status="cancelled",
                details={
                    "action": "superseded_by_conflict_resolution",
                    "child_plan_id": new_plan["plan_id"],
                    "resolution_artifact": resolution_artifact.as_mapping(),
                    "provider_calls": 0,
                },
            )
        with self.services.projects.store.transaction() as connection:
            context = {"actor_id": row["actor_id"], "project_id": project_id}
            connection.execute(
                "UPDATE manual_command_confirmations SET status='consumed', consumed_at=? WHERE command_id=? AND status='pending'",
                (utc_now(), command_id),
            )
            connection.execute(
                "UPDATE manual_command_idempotency SET status='consumed', updated_at=? WHERE command_id=?",
                (utc_now(), command_id),
            )
            _manual_audit(connection, command_id, context, tool_name, "completed", digest)
        return {
            "command_id": command_id,
            "status": "completed",
            "resolution_artifact": resolution_artifact.as_mapping(),
            "derived_brief_version_id": derived_brief_version_id,
            "new_plan": new_plan,
            "model_calls": 0,
            "script_versions_created": 0,
        }

    def _authoritative_conflict_resolutions(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        plan_id: str,
        plan_sha256: str,
        expected_project_version: int,
        resolutions: list[dict],
    ) -> dict:
        preview = self.get_plan_preview(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            plan_id=plan_id,
            expected_project_version=expected_project_version,
        )
        if preview["plan_sha256"] != plan_sha256:
            raise WorkspaceServiceError("PLAN_ARTIFACT_DRIFT", "Plan SHA 已变化，请重新核对冲突")
        by_id = {
            str(item.get("conflict_id") or ""): item
            for item in preview.get("conflicts") or []
            if item.get("status") == "unresolved"
        }
        if any(item.get('type') == 'creative_brief_flow_conflict' for item in by_id.values()):
            raise WorkspaceServiceError('CREATIVE_BRIEF_FLOW_CONFLICT',
                '初稿与已确认流程顺序不一致，请返回修改初稿，或通过资料确认入口调整流程')
        choices = {}
        for raw in resolutions:
            conflict_id = str(raw.get("conflict_id") or "") if isinstance(raw, dict) else ""
            choice = str(raw.get("choice") or "") if isinstance(raw, dict) else ""
            if conflict_id not in by_id or choice not in {"brief_wins", "template_wins"}:
                raise WorkspaceServiceError("INVALID_INPUT", "冲突 ID 或处理选择无效")
            if conflict_id in choices:
                raise WorkspaceServiceError("INVALID_INPUT", "同一冲突不能重复选择")
            choices[conflict_id] = choice
        if not choices or set(choices) != set(by_id):
            raise WorkspaceServiceError("INVALID_INPUT", "必须一次解决当前 Plan 的全部未解决冲突")
        authoritative = []
        for conflict_id in sorted(choices):
            conflict = by_id[conflict_id]
            evidence = next(
                (
                    item for item in conflict.get("evidence_refs") or []
                    if item.get("fact_id") and item.get("text")
                ),
                {},
            )
            authoritative.append({
                "conflict_id": conflict_id,
                "choice": choices[conflict_id],
                "requirement_id": str(conflict.get("requirement_id") or ""),
                "shot_no": str(conflict.get("shot_no") or ""),
                "field": str(conflict.get("field") or "structure"),
                "rule_id": str(conflict.get("rule_id") or ""),
                "rule_level": str(conflict.get("rule_level") or "structural_required"),
                "brief_value": str(conflict.get("brief_value") or ""),
                "template_value": str(conflict.get("template_value") or ""),
                "fact_id": str(evidence.get("fact_id") or ""),
                "original_text": str(evidence.get("text") or conflict.get("brief_value") or ""),
                "evidence_refs": list(conflict.get("evidence_refs") or []),
            })
        paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
        plan_event = self._plan_event(auth_token, plan_id)
        plan_run_id = self._run_id_from_ref(plan_event["plan_ref"])
        stored_plan = read_script_plan(
            self.services.artifacts.resolve_stage_artifact(plan_run_id, "script_plan").path
        )
        return {
            "resolutions": authoritative,
            "binding": {
                "project_version": expected_project_version,
                "brief_sha256": paired.brief.payload.sha256,
                "template_rules_sha256": str(
                    (stored_plan.template_rule_contract or {}).get("sha256") or ""
                ),
            },
        }

    def current_confirmed_brief(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int | None = None,
    ) -> ConfirmedBriefInput:
        """Read the current formal input server-side without exposing it to a page."""
        before = self._current_plan_state(
            auth_token, workspace_id, project_id, expected_project_version
        )
        paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
        confirmed = self._current_confirmed_brief(
            auth_token, workspace_id, project_id, before["brief_snapshot_id"]
        )
        after = self._current_plan_state(
            auth_token, workspace_id, project_id, expected_project_version
        )
        if after != before:
            raise FormalGenerationBlocked(
                "PROJECT_STATE_CHANGED", "读取当前正式 Brief 期间项目锁或快照已变化"
            )
        self._require_same_current_pair(auth_token, workspace_id, project_id, paired)
        return confirmed

    def get_image_readiness(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        expected_project_version: int | None = None,
    ) -> dict:
        before = self._current_plan_state(auth_token, workspace_id, project_id, expected_project_version)
        paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
        readiness = self._resolve_images(
            auth_token, workspace_id=workspace_id, project_id=project_id, paired=paired,
            expected_project_version=before["project_version"],
        )
        if self._current_plan_state(auth_token, workspace_id, project_id, before["project_version"]) != before:
            raise FormalGenerationBlocked("PROJECT_STATE_CHANGED", "读取图片期间项目锁或当前 Brief 已变化")
        self._require_same_current_pair(auth_token, workspace_id, project_id, paired)
        assert isinstance(self.services.images, ProjectImagePoolService)
        result = self.services.images.employee_readiness(readiness)
        result["unmaterialized_source_count"] = self.services.images.pending_source_materialization(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            brief_version_id=paired.brief.brief_version_id,
            brief_snapshot_id=paired.brief.brief_snapshot_id,
            expected_project_lock_version=before["project_version"],
        )
        from .creative_generation import repository
        creative = repository(self, auth_token, workspace_id, project_id).read()["body"]
        active_blocks = {row['shot_id'] for row in (creative.get('draft') or {}).get('shots', [])}
        references = [ref for ref in creative.get('image_references', []) if ref['block_id'] in active_blocks]
        result['creative_reference_count'] = len(references)
        result['creative_reference_block_count'] = len({ref['block_id'] for ref in references})
        with self.services.projects.store.connection() as connection:
            registered = {row[0] for row in connection.execute(
                "SELECT task_id FROM project_image_generated_sources WHERE project_image_id IN (SELECT project_image_id FROM project_images WHERE project_id=?)", (project_id,))}
        missing_references = len({item['task_id'] for item in references if item.get('task_id')} - registered)
        result["unmaterialized_creative_reference_count"] = missing_references
        result["unmaterialized_source_count"] += missing_references
        return result

    def synchronize_image_sources(self, auth_token: str, *, workspace_id: str,
            project_id: str, expected_project_version: int) -> dict:
        before = self._current_plan_state(auth_token, workspace_id, project_id, expected_project_version)
        paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
        from .creative_generation import repository
        from .creative_images import register_reference
        references = repository(self, auth_token, workspace_id, project_id).read()["body"].get("image_references", [])
        for reference in references:
            if not reference.get('task_id'):
                continue
            register_reference(self, auth_token, workspace_id=workspace_id, project_id=project_id,
                task_id=reference['task_id'], brief_version_id=paired.brief.brief_version_id,
                brief_snapshot_id=paired.brief.brief_snapshot_id, expected_project_version=before["project_version"])
        self._synchronize_and_resolve_images(auth_token, workspace_id=workspace_id,
            project_id=project_id, paired=paired, expected_project_version=before["project_version"])
        return self.get_image_readiness(auth_token, workspace_id=workspace_id,
            project_id=project_id, expected_project_version=expected_project_version)

    def analyze_pending_images(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        expected_project_version: int,
    ) -> dict:
        """Compatibility entrypoint delegates to the same durable batch authority."""
        from uuid import uuid4
        result = self.start_image_analysis_batch(auth_token, workspace_id=workspace_id,
            project_id=project_id, expected_project_version=expected_project_version,
            idempotency_key='legacy-image-analysis-' + uuid4().hex)
        if result['task']['status'] == 'executing':
            result = self.execute_image_analysis_batch(auth_token, task_id=result['task']['task_id'])
        project = self.services.projects.get_project(auth_token, project_id)
        response = self.get_image_readiness(auth_token, workspace_id=workspace_id,
            project_id=project_id, expected_project_version=project['project_version'])
        progress = result['response']['progress']
        response['automatic_understanding'] = {
            'processed': progress['processed'], 'remaining': response['pending'],
            'needs_review': progress['needs_review'], 'failed': progress['failed'],
            'has_more': response['pending'] > 0, 'task_id': result['task']['task_id'],
        }
        response['analysis_batch'] = result['response']
        return response

    def confirm_image_analyses(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        expected_project_version: int, selections: list[dict], note: str, idempotency_key: str,
    ) -> dict:
        assert isinstance(self.services.images, ProjectImagePoolService)
        command = ManualProjectImageAnalysisCommandService(self.services.images)
        command.confirm_existing_analyses(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_lock_version=expected_project_version, selections=selections,
            note=note, idempotency_key=idempotency_key,
        )
        return self.read_image_analysis_confirmation(auth_token, workspace_id=workspace_id,
            project_id=project_id, idempotency_key=idempotency_key)

    def read_image_analysis_confirmation(self, auth_token: str, *, workspace_id: str,
                                         project_id: str, idempotency_key: str) -> dict:
        command = ManualProjectImageAnalysisCommandService(self.services.images)
        result = command.read_confirmation(auth_token, workspace_id=workspace_id,
            project_id=project_id, idempotency_key=idempotency_key)
        if result["confirmation_status"] != "completed":
            return result
        project = self.services.projects.get_project(auth_token, project_id)
        return {**self.get_image_readiness(auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=project["project_version"]), "project_version": project["project_version"], **result}

    def record_image_analysis(
        self, auth_token: str, *, workspace_id: str, project_id: str, image_id: str,
        expected_project_version: int, status: str, caption: str, tags: tuple[str, ...],
        subjects: tuple[str, ...], scene: str, purpose: str, quality: str, brand: str,
        evidence: tuple[str, ...], quality_flags: tuple[str, ...] = (),
        negative_flags: tuple[str, ...] = (), expected_analysis_key: str | None = None,
    ) -> dict:
        before = self._current_plan_state(auth_token, workspace_id, project_id, expected_project_version)
        paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
        readiness = self._synchronize_and_resolve_images(
            auth_token, workspace_id=workspace_id, project_id=project_id, paired=paired,
            expected_project_version=expected_project_version,
        )
        image = next((item for item in readiness.images if item.image_id == image_id), None)
        if image is None:
            self._reject_noncurrent_image(image_id)
        assert isinstance(self.services.images, ProjectImagePoolService)
        ManualProjectImageAnalysisCommandService(self.services.images).record_analysis(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_lock_version=expected_project_version,
            expected_analysis_key=expected_analysis_key,
            submission=ManualImageAnalysisInput(
                image_id=image.image_id, image_sha256=image.image_sha256,
                brief_version_id=paired.brief.brief_version_id,
                brief_snapshot_id=paired.brief.brief_snapshot_id, status=status,
                caption=caption, tags=tags, subjects=subjects, scene=scene, purpose=purpose,
                quality=quality, brand=brand, evidence=evidence,
                quality_flags=quality_flags, negative_flags=negative_flags,
            ),
        )
        self._require_same_current_pair(auth_token, workspace_id, project_id, paired)
        if self._current_plan_state(auth_token, workspace_id, project_id, expected_project_version) != before:
            raise FormalGenerationBlocked("PROJECT_STATE_CHANGED", "人工分析期间项目锁或当前 Brief 已变化")
        return self.get_image_readiness(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=expected_project_version,
        )

    def read_image_preview(
        self, auth_token: str, *, workspace_id: str, project_id: str, image_id: str,
        expected_project_version: int, variant: str = "original", version_id: str = "",
    ) -> tuple[bytes, str]:
        if version_id:
            from .version_delivery import VersionDeliveryService
            project = self.services.projects.get_project(auth_token, project_id)
            if project["workspace_id"] != workspace_id:
                raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
            workspace = getattr(getattr(self, "creative_revision", None), "image_workspace", None)
            delivery = workspace.services.delivery if workspace is not None else getattr(self, "_preview_delivery", None)
            if delivery is None:
                delivery = self._preview_delivery = VersionDeliveryService(self.services.projects.store, self.services.artifacts)
            context = delivery.resolve(auth_token, project_id, version_id,
                expected_project_version=expected_project_version, ancestor_of=project["current_version_id"], reuse_immutable=True)
            plan = read_script_plan(self.services.artifacts.resolve_stage_artifact(
                context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256).path)
            evidence = plan.confirmed_brief_input
            if evidence.get("workspace_id") != workspace_id or evidence.get("project_id") != project_id:
                raise WorkspaceServiceError("FORMAL_BRIEF_DRIFT", "版本 Brief 不属于当前项目")
            images = self.services.images
            if not isinstance(images, ProjectImagePoolService):
                raise FormalGenerationBlocked("FORMAL_IMAGE_AUTHORITY_UNAVAILABLE", "正式图片权威服务未安装")
            value = images.read_preview_image(auth_token, workspace_id=workspace_id, project_id=project_id,
                image_id=image_id, brief_version_id=evidence["brief_version_id"],
                brief_snapshot_id=evidence["brief_snapshot_id"], expected_project_lock_version=expected_project_version,
                variant=variant, historical=True)
            delivery.resolve(auth_token, project_id, version_id, expected_project_version=expected_project_version,
                ancestor_of=project["current_version_id"], reuse_immutable=True)
            return value
        paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
        images = self.services.images
        if not isinstance(images, ProjectImagePoolService):
            raise FormalGenerationBlocked("FORMAL_IMAGE_AUTHORITY_UNAVAILABLE", "正式图片规划门未注入项目图片权威服务")
        try:
            value = images.read_preview_image(
                auth_token, workspace_id=workspace_id, project_id=project_id, image_id=image_id,
                brief_version_id=paired.brief.brief_version_id, brief_snapshot_id=paired.brief.brief_snapshot_id,
                expected_project_lock_version=expected_project_version, variant=variant,
            )
        except WorkspaceServiceError as exc:
            if exc.code == "NOT_FOUND":
                self._reject_noncurrent_image(image_id)
            if str(exc.code).startswith("IMAGE_"):
                raise FormalGenerationBlocked("FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED",
                    "当前目标图片证据已漂移，必须重新人工核验") from exc
            raise
        self._require_same_current_pair(auth_token, workspace_id, project_id, paired)
        return value

    def list_waiting_plans(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int | None = None,
    ) -> dict:
        """Project-read seam for recovering opaque plan IDs without exposing evidence."""
        before = self._current_plan_state(
            auth_token, workspace_id, project_id, expected_project_version
        )
        paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
        with self.services.briefs.store.connection() as connection:
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                permission="project.read",
            )
            rows = connection.execute(
                """
                SELECT t.task_id, t.status
                FROM tasks t
                JOIN agent_sessions s ON s.agent_session_id = t.agent_session_id
                WHERE t.project_id = ? AND s.project_id = ?
                  AND t.task_type = 'formal_generation_plan'
                  AND t.actor_id = ? AND s.actor_id = t.actor_id
                ORDER BY t.created_at DESC, t.task_id DESC
                """,
                (project_id, project_id, actor["actor_id"]),
            ).fetchall()
        items, unavailable = [], []
        for row in rows:
            # Executed or failed plans supersede older proposals for automatic recovery.
            # Explicit historical previews and all frozen evidence remain available.
            if row["status"] != "waiting_user":
                if not items and row["status"] in {"blocked", "failed", "cancelled"}:
                    try:
                        event = self._plan_event(auth_token, row["task_id"])
                    except FormalGenerationBlocked as exc:
                        if exc.code != "PLAN_CONFIRMATION_REQUIRED":
                            raise
                        event = {}
                    unavailable.append({"plan_id": row["task_id"], "status": row["status"],
                        "code": "PLAN_NOT_WAITING_CONFIRMATION",
                        "uses_creative_draft": bool(event.get("creative_draft_binding")),
                        "message": "最近一次规划未完成生成，记录已保留；请按当前资料重新生成规划。"})
                break
            try:
                # Use the same evidence and input checks as preview/confirmation.
                self.get_plan_preview(auth_token, workspace_id=workspace_id,
                    project_id=project_id, plan_id=row["task_id"],
                    expected_project_version=before["project_version"])
            except FormalGenerationBlocked as exc:
                stale = exc.code in {
                    "PLAN_INPUTS_STALE", "CREATIVE_PLAN_STALE",
                    "FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED", "KNOWLEDGE_RULE_DRIFT",
                }
                if not stale and exc.code not in {
                    "PLAN_EVENT_INVALID", "PLAN_ARTIFACT_DRIFT", "PLAN_ARTIFACT_UNAVAILABLE",
                    "ARTIFACT_DRIFT", "ARTIFACT_NOT_FOUND", "MANIFEST_DRIFT",
                    "ARTIFACT_HASH_MISMATCH", "MANIFEST_HASH_MISMATCH", "ARTIFACT_INVALID", "NOT_FOUND",
                    "ASSET_PACKAGE_DRIFT", "PLAN_NOT_WAITING_CONFIRMATION",
                }:
                    raise
                unavailable.append({"plan_id": row["task_id"],
                    "status": "stale" if stale else "blocked", "code": exc.code,
                    "uses_creative_draft": bool(self._plan_event(auth_token, row["task_id"]).get("creative_draft_binding")),
                    "message": exc.message if stale else "旧规划证据不可用，不能继续确认；历史已保留，可按当前资料重新规划。"})
            else:
                items.append({"plan_id": row["task_id"], "status": "waiting_user"})
        after = self._current_plan_state(
            auth_token, workspace_id, project_id, expected_project_version
        )
        if after != before:
            raise FormalGenerationBlocked(
                "PROJECT_STATE_CHANGED", "读取规划列表期间项目锁或当前 Brief 已变化"
            )
        self._require_same_current_pair(auth_token, workspace_id, project_id, paired)
        return {"items": items, **({"unavailable_items": unavailable} if unavailable else {})}

    def get_planning_readiness(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        expected_project_version: int | None = None,
    ) -> dict:
        """Read the same confirmed-Brief prerequisite used by prepare_plan, without tasks or writes."""
        before = self._current_plan_state(auth_token, workspace_id, project_id, expected_project_version)
        paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
        result = {"ready": True, "code": "", "message": "Brief 规划字段已核对。"}
        try:
            self._planning_brief(auth_token, workspace_id, project_id, paired.brief)
        except FormalGenerationBlocked as exc:
            if exc.code != "confirmed_brief_needs_clarification":
                raise
            result = {"ready": False, "code": exc.code, "message": exc.message}
        after = self._current_plan_state(auth_token, workspace_id, project_id, expected_project_version)
        if before != after:
            raise FormalGenerationBlocked("PROJECT_STATE_CHANGED", "核对期间项目资料已变化，请刷新重试。")
        self._require_same_current_pair(auth_token, workspace_id, project_id, paired)
        return result

    def list_project_activity(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
    ) -> dict:
        """Employee-safe project task timeline backed by persisted TaskService rows."""
        project = self.services.projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise FormalGenerationBlocked(
                "CROSS_PROJECT_DENIED", "project 不属于目标 workspace",
            )
        items = []
        for row in self.services.tasks.list_project_tasks(auth_token, project_id):
            if row["task_type"] != "formal_generation_plan":
                continue
            items.append({
                **{key: row[key] for key in ("title", "state_label", "next_action", "issue", "action_label", "action_href")},
                "state": str(row["status"]),
                "issue_code": str(row.get("error_code") or ""),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            })
        return {
            "items": items,
            "count": len(items),
            "production_certification": "BLOCK",
        }

    def generate_from_confirmed_plan(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        task_id: str,
        plan_ref: str,
        plan_sha256: str,
        expected_project_version: int,
        _recovery_attempt_id: str = "",
    ) -> dict:
        """R2: only this explicit matching confirmation may invoke text providers."""
        project = self._project_at_lock(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        self._require_no_unsaved_script_draft(project)
        parent_version_id = str(project.get("current_version_id") or "") or None
        paired = self._resolve_current_paired(auth_token, workspace_id, project_id)
        images = self._resolve_images(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            paired=paired, expected_project_version=expected_project_version,
        )
        event = self._plan_event(auth_token, task_id)
        self._require_plan_task(auth_token, project_id, task_id)
        self._require_current_plan_pair(auth_token, event, paired)
        creative_formal_binding.require_current(self, auth_token, workspace_id=workspace_id,
            project_id=project_id, binding=event.get("creative_draft_binding"))
        generation_mode = str(event.get("generation_mode") or "")
        self._require_images_ready(images, generation_mode=generation_mode)
        image_evidence = self._image_event_evidence(images)
        planning_brief = self._planning_brief(
            auth_token, workspace_id, project_id, paired.brief,
        )
        task = self.services.tasks.get_task(auth_token, task_id)
        if task["project_id"] != project_id or task["task_type"] != "formal_generation_plan":
            raise FormalGenerationBlocked("CROSS_PROJECT_DENIED", "规划 task 不属于当前项目")
        expected_statuses = {"blocked", "failed"} if _recovery_attempt_id else {"waiting_user"}
        if task["status"] not in expected_statuses:
            raise FormalGenerationBlocked("PLAN_CONFIRMATION_REQUIRED", "规划尚未处于待用户确认状态")
        asset_package, package_evidence = self._asset_package_for(
            planning_brief["脚本类型"], project=project, frozen_evidence=event.get("formal_asset_package")
        )
        effective_knowledge = self._effective_knowledge(auth_token, project_id)
        self._validate_plan_event(
            event, paired, image_evidence, package_evidence, effective_knowledge,
        )
        generation_mode = str(event.get("generation_mode") or "")
        if generation_mode not in {"image_backed", "text_only_draft"}:
            raise FormalGenerationBlocked("PLAN_EVENT_INVALID", "规划缺少显式图片策略")
        if images.total == 0 and generation_mode != "text_only_draft":
            raise FormalGenerationBlocked("IMAGE_STRATEGY_CONFIRMATION_REQUIRED", "零图片项目未确认文字草稿模式")
        if event.get("plan_ref") != plan_ref or event.get("plan_sha256") != plan_sha256:
            raise FormalGenerationBlocked("PLAN_CONFIRMATION_MISMATCH", "必须确认同一份规划 ref 与 SHA-256")
        parent_run_id = self._run_id_from_ref(plan_ref)
        try:
            parent_manifest = self.services.artifacts.resolve_manifest(parent_run_id)
            parent_plan = self.services.artifacts.resolve_stage_artifact(
                parent_run_id, "script_plan", expected_manifest_sha256=parent_manifest.sha256
            )
        except ArtifactResolutionError as exc:
            self._block_task(auth_token, task, exc.code, {"action": "generate_from_confirmed_plan", "provider_calls": 0})
            raise self._as_blocked(exc) from exc
        if parent_plan.ref != plan_ref or parent_plan.sha256 != plan_sha256:
            self._block_task(auth_token, task, "PLAN_ARTIFACT_DRIFT", {"action": "generate_from_confirmed_plan", "provider_calls": 0})
            raise FormalGenerationBlocked("PLAN_ARTIFACT_DRIFT", "已确认规划 artifact 已变化")
        try:
            parent_raw = json.loads(parent_plan.path.read_text(encoding="utf-8"))
            if sha256_file(parent_plan.path) != parent_plan.sha256:
                raise FormalGenerationBlocked("PLAN_ARTIFACT_DRIFT", "已确认规划 artifact 在读取期间已变化")
            self._validate_plan_artifact_binding(
                parent_raw,
                paired,
                image_evidence,
                package_evidence,
                effective_knowledge,
            )
            creative_binding = parent_raw.get("constraints", {}).get("creative_draft_binding")
            if creative_binding != event.get("creative_draft_binding"):
                raise FormalGenerationBlocked("PLAN_EVENT_INVALID", "初稿绑定与规划证据不一致")
            creative_formal_binding.require_current(self, auth_token, workspace_id=workspace_id,
                project_id=project_id, binding=creative_binding)
            planning_status = str(
                (parent_raw.get("planning_audit") or {}).get("status") or ""
            )
            if planning_status == "needs_clarification":
                raise FormalGenerationBlocked(
                    "PLANNING_NEEDS_CLARIFICATION",
                    "规划仍有待确认事实；未调用写稿或编辑模型，也未创建脚本版本",
                )
            if planning_status != "pass":
                raise FormalGenerationBlocked(
                    "PLANNING_AUDIT_BLOCKED", "规划机器审计未通过"
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError, FormalGenerationBlocked) as exc:
            self._block_task(auth_token, task, self._error_code(exc), {"action": "generate_from_confirmed_plan", "provider_calls": 0})
            raise self._as_blocked(exc) from exc
        self._require_same_current_pair(auth_token, workspace_id, project_id, paired)
        self._require_same_images(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            paired=paired, expected_project_version=expected_project_version,
            expected=image_evidence, generation_mode=generation_mode,
        )
        self._require_same_generation_inputs(
            auth_token,
            project_id=project_id,
            script_type=planning_brief["脚本类型"],
            expected_package=package_evidence,
            expected_knowledge=effective_knowledge,
        )
        if self.provider_policy is None:
            self._block_task(auth_token, task, "FORMAL_PROVIDER_UNAVAILABLE", {"action": "generate_from_confirmed_plan", "provider_calls": 0})
            raise FormalGenerationBlocked("FORMAL_PROVIDER_UNAVAILABLE", "服务端未配置受控 client-review Provider，未生成伪脚本")

        if not _recovery_attempt_id:
            task = self.services.tasks.transition_task(
                auth_token,
                task_id=task_id,
                expected_task_version=task["task_version"],
                status="executing",
                details={"action": "generate_from_confirmed_plan", "plan_ref": plan_ref, "plan_sha256": plan_sha256},
            )
        policy = self.provider_policy
        calls: list[tuple[str, str]] = []

        def limited(name: str, provider: TextProvider) -> TextProvider:
            def invoke(prompt: str, model: str, max_tokens: int) -> str:
                if max_tokens != MAX_OUTPUT_TOKENS:
                    raise RuntimeError("Provider max_tokens 未沿用既有常量")
                if len(calls) >= 2:
                    raise RuntimeError("client-review Provider live 调用超过 2 次")
                calls.append((name, model))
                return provider(prompt, model, max_tokens)
            return invoke

        planning_brief = read_script_plan(parent_plan.path).brief
        source_root = self._source_root_for(
            planning_brief["脚本类型"], asset_package=asset_package,
        )
        cache_root = self._project_cache(project_id)
        try:
            with fork_pipeline_run(
                parent_run_id,
                cache_root,
                brief=planning_brief,
                source_root=source_root,
                cache_fingerprint=sha256_json({
                    "confirmed_plan": plan_sha256,
                    "route": "client-review",
                    "formal_asset_package": package_evidence,
                    "effective_knowledge": effective_knowledge,
                }),
                from_stage="script_draft",
                provenance={
                    "formal_asset_package": package_evidence,
                    "effective_knowledge": effective_knowledge,
                    **({"recovery_attempt_id": _recovery_attempt_id} if _recovery_attempt_id else {}),
                },
            ) as state:
                iteration = run_generation_once(
                    [],
                    source_root,
                    state.run_dir / "formal-script.xlsx",
                    state.run_dir / "formal-script.audit.json",
                    source_view="ai-spec",
                    asset_package=asset_package,
                    quality_profile="client-review",
                    text_model=policy.editor_model,
                    writer_provider=policy.writer_provider,
                    writer_model=policy.writer_model,
                    editor_provider=policy.editor_provider,
                    editor_model=policy.editor_model,
                    writer_text_provider=limited("writer", policy.writer),
                    editor_text_provider=limited("editor", policy.editor),
                    runtime=state,
                )
                if iteration.audit.get("status") != "pass":
                    blocking = "、".join(iteration.audit.get("blocking_items") or [])
                    raise FormalGenerationBlocked(
                        "FORMAL_GENERATION_AUDIT_FAILED",
                        f"生成脚本未通过机器审计{f'：{blocking}' if blocking else ''}",
                    )
                self._require_same_generation_inputs(
                    auth_token,
                    project_id=project_id,
                    script_type=planning_brief["脚本类型"],
                    expected_package=package_evidence,
                    expected_knowledge=effective_knowledge,
                )
                creative_formal_binding.require_current(self, auth_token, workspace_id=workspace_id,
                    project_id=project_id, binding=creative_binding)
                finalize_pipeline_run(
                    state,
                    "completed",
                    {"workbook": iteration.output_path, "audit": iteration.audit_path},
                    metrics={"provider_calls": len(calls), "writer_provider": policy.writer_provider, "writer_model": policy.writer_model, "editor_provider": policy.editor_provider, "editor_model": policy.editor_model},
                )
                run_id = state.run_id
            manifest = self.services.artifacts.resolve_manifest(run_id)
            workbook = self.services.artifacts.resolve_artifact(run_id, "workbook")
            draft = self.services.artifacts.resolve_stage_artifact(run_id, "script_draft", expected_manifest_sha256=manifest.sha256)
        except Exception as exc:
            self._block_task(auth_token, task, self._error_code(exc), {"action": "generate_from_confirmed_plan", "provider_calls": len(calls)})
            raise self._as_blocked(exc) from exc

        self._require_same_current_pair(auth_token, workspace_id, project_id, paired)
        self._require_same_images(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            paired=paired, expected_project_version=expected_project_version,
            expected=image_evidence, generation_mode=generation_mode,
        )

        bound = self.services.projects.bind_run(
            auth_token,
            project_id=project_id,
            run_id=run_id,
            source_kind="native",
            manifest_ref=manifest.ref,
            manifest_sha256=manifest.sha256,
            expected_project_version=expected_project_version,
            parent_run_id=parent_run_id,
        )
        self._require_same_current_pair(auth_token, workspace_id, project_id, paired)
        version = self.services.projects.create_project_version(
            auth_token,
            project_id=project_id,
            run_id=run_id,
            content_sha256=workbook.sha256,
            artifact_ref=workbook.ref,
            expected_project_version=bound["project_version"],
            parent_version_id=parent_version_id,
        )
        with self.services.projects.store.transaction() as connection:
            connection.execute(
                """INSERT INTO version_completion_states(
                    completion_state_id, workspace_id, project_id, version_id,
                    generation_mode, image_state, review_eligible, delivery_eligible,
                    evidence_ref, evidence_sha256, created_by_actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, 'images_pending', 0, 0, ?, ?, ?, ?)""",
                (
                    new_id("completion_state"), workspace_id, project_id, version["version_id"],
                    generation_mode, draft.ref, draft.sha256, task["actor_id"], utc_now(),
                ),
            )
        if not _recovery_attempt_id:
            task = self.services.tasks.transition_task(
                auth_token,
                task_id=task_id,
                expected_task_version=task["task_version"],
                status="completed",
                result_ref=draft.ref,
                details={
                    "action": "generate_from_confirmed_plan",
                    "plan_ref": plan_ref,
                    "plan_sha256": plan_sha256,
                    "run_id": run_id,
                    "manifest_ref": manifest.ref,
                    "manifest_sha256": manifest.sha256,
                    "version_id": version["version_id"],
                    "provider_calls": len(calls),
                    "writer_provider": policy.writer_provider,
                    "writer_model": policy.writer_model,
                    "editor_provider": policy.editor_provider,
                    "editor_model": policy.editor_model,
                    "project_image_context": image_evidence,
                    "generation_mode": generation_mode,
                    "formal_asset_package": package_evidence,
                    "effective_knowledge": effective_knowledge,
                    **self._paired_event_evidence(paired),
                },
            )
        completed_draft = read_script_draft(draft.path)
        inherited_images = {"status": "not_required", "count": 0}
        if creative_binding and (creative_binding.get("image_references") or creative_binding.get("protected_block_ids")):
            try:
                inherited_images = self.inherit_creative_images(auth_token, workspace_id=workspace_id,
                    project_id=project_id, version_id=version["version_id"])
            except Exception:
                # The formal version is already durable. Preserve it and expose
                # a retry of local adoption, never a second text/image request.
                inherited_images = {"status": "pending", "message": "正式脚本已保存；初稿参考图和保护范围仍保留，请在配图页面继续接入并核对。"}
        return {
            "creative_images": inherited_images,
            "plan_id": task_id,
            "status": "completed",
            "version_id": version["version_id"],
            "script_rows": completed_draft.script_rows,
            "pending_confirmations": completed_draft.pending_confirmations,
            "traceability": completed_draft.traceability,
            **({"workbook_ref": workbook.ref, "workbook_sha256": workbook.sha256} if _recovery_attempt_id else {}),
        }

    def recover_confirmed_generation(self, context) -> dict:
        """Execute a confirmed retry on the original frozen plan and generation core."""
        from .identity_service import require_permission

        with self.services.projects.store.connection() as connection:
            row = connection.execute(
                """SELECT p.*, e.confirmed_by_actor_id
                FROM recovery_executions e JOIN recovery_proposals p
                  ON p.recovery_proposal_id=e.recovery_proposal_id
                WHERE e.new_attempt_id=? AND p.task_id=?""",
                (context.attempt_id, context.task_id),
            ).fetchone()
            if row is None or row["requested_stage_id"] != "formal_generation_plan":
                raise FormalGenerationBlocked("PLAN_CONFIRMATION_REQUIRED", "生成重试缺少已确认的恢复提案")
            actor = require_permission(connection, context.auth_token, workspace_id=row["workspace_id"],
                                       project_id=row["project_id"], permission="run.retry")
            if actor["actor_id"] != row["confirmed_by_actor_id"]:
                raise FormalGenerationBlocked("PERMISSION_DENIED", "生成重试必须使用原确认会话")
            parameters = dict(row)
        event = self._plan_event(context.auth_token, context.task_id)
        # A completed version can outlive the executor's trailing receipt.
        # Reconcile its explicit attempt provenance before invoking models again.
        with self.services.projects.store.connection() as connection:
            versions = connection.execute(
                "SELECT * FROM project_versions WHERE project_id=? ORDER BY created_at DESC",
                (parameters["project_id"],),
            ).fetchall()
        for version in versions:
            manifest = self.services.artifacts.read_manifest(version["run_id"])
            if (manifest.get("run_provenance") or {}).get("recovery_attempt_id") != context.attempt_id:
                continue
            workbook = self.services.artifacts.resolve_artifact(version["run_id"], "workbook")
            if (version["artifact_ref"], version["content_sha256"]) != (workbook.ref, workbook.sha256):
                raise FormalGenerationBlocked("PLAN_ARTIFACT_DRIFT", "重试版本与冻结产物不一致")
            return {"status": "succeeded", "input_ref": event["plan_ref"], "input_sha256": event["plan_sha256"],
                    "output_ref": workbook.ref, "output_sha256": workbook.sha256}
        result = self.generate_from_confirmed_plan(
            context.auth_token, workspace_id=parameters["workspace_id"], project_id=parameters["project_id"],
            task_id=context.task_id, plan_ref=event["plan_ref"], plan_sha256=event["plan_sha256"],
            expected_project_version=parameters["expected_project_version"],
            _recovery_attempt_id=context.attempt_id,
        )
        return {"status": "succeeded", "input_ref": event["plan_ref"], "input_sha256": event["plan_sha256"],
                "output_ref": result["workbook_ref"], "output_sha256": result["workbook_sha256"]}

    def confirm_plan_and_generate(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        plan_id: str,
        expected_project_version: int,
    ) -> dict:
        """Page-safe R2 seam: the browser confirms an opaque plan identifier only."""
        details = self._plan_event(auth_token, plan_id)
        return self.generate_from_confirmed_plan(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            task_id=plan_id,
            plan_ref=str(details["plan_ref"]),
            plan_sha256=str(details["plan_sha256"]),
            expected_project_version=expected_project_version,
        )

    def _planning_brief(self, auth_token: str, workspace_id: str, project_id: str, confirmed: ConfirmedBriefInput) -> dict:
        if not isinstance(confirmed, ConfirmedBriefInput):
            raise FormalGenerationBlocked("CONFIRMED_BRIEF_REQUIRED", "只接受 FormalBriefWorkspaceService 返回的 ConfirmedBriefInput")
        if (confirmed.workspace_id, confirmed.project_id) != (workspace_id, project_id):
            raise FormalGenerationBlocked("CROSS_PROJECT_DENIED", "Confirmed Brief 不属于当前项目或工作区")
        try:
            payload = self.services.briefs.resolve_confirmed(auth_token, workspace_id=workspace_id, project_id=project_id, confirmed=confirmed)
            project = self.services.projects.get_project(auth_token, project_id)
            context = {"authority": "projects.name", "workspace_id": workspace_id,
                       "project_id": project_id, "name": project["name"],
                       "project_version": project["project_version"]}
            return _planning_brief_from_confirmed_payload(
                payload, _confirmed_brief_evidence(confirmed), project_context=context,
            )[0]
        except Exception as exc:
            raise self._as_blocked(exc) from exc

    def _current_plan_state(
        self,
        auth_token: str,
        workspace_id: str,
        project_id: str,
        expected_project_version: int | None,
    ) -> dict:
        project = self.services.projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise FormalGenerationBlocked(
                "CROSS_PROJECT_DENIED", "project 不属于目标 workspace"
            )
        if expected_project_version is not None:
            if (
                type(expected_project_version) is not int
                or expected_project_version < 0
                or project["project_version"] != expected_project_version
            ):
                raise FormalGenerationBlocked(
                    "PROJECT_VERSION_CONFLICT", "project lock 已变化，必须刷新后重试"
                )
        snapshot_id = str(project.get("current_brief_snapshot_id") or "")
        if not snapshot_id:
            raise FormalGenerationBlocked(
                "CURRENT_BRIEF_MISSING", "当前项目没有已确认 Brief 快照"
            )
        return {
            "project_version": project["project_version"],
            "brief_snapshot_id": snapshot_id,
        }

    def _require_plan_task(self, auth_token: str, project_id: str, plan_id: str) -> dict:
        task = self.services.tasks.get_task(auth_token, plan_id)
        if (
            task["project_id"] != project_id
            or task["task_type"] != "formal_generation_plan"
        ):
            raise FormalGenerationBlocked(
                "CROSS_PROJECT_DENIED", "规划 task 不属于当前项目"
            )
        with self.services.briefs.store.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM agent_sessions
                WHERE agent_session_id = ? AND project_id = ?
                """,
                (task["agent_session_id"], project_id),
            ).fetchone()
        if row is None:
            raise FormalGenerationBlocked(
                "PLAN_EVENT_INVALID", "规划 task 的 agent session 不属于当前项目"
            )
        return task

    def _current_confirmed_brief(
        self,
        auth_token: str,
        workspace_id: str,
        project_id: str,
        brief_snapshot_id: str,
    ) -> ConfirmedBriefInput:
        with self.services.briefs.store.connection() as connection:
            row = connection.execute(
                """
                SELECT c.*, v.payload_ref, v.payload_sha256
                FROM brief_confirmations c
                JOIN brief_versions v ON v.brief_version_id = c.brief_version_id
                JOIN project_brief_snapshots s ON s.brief_snapshot_id = c.brief_snapshot_id
                WHERE c.brief_snapshot_id = ? AND c.workspace_id = ?
                  AND c.project_id = ? AND s.project_id = ?
                """,
                (brief_snapshot_id, workspace_id, project_id, project_id),
            ).fetchone()
        if row is None:
            raise FormalGenerationBlocked(
                "CURRENT_BRIEF_MISMATCH", "当前 Brief 快照不是可用的正式确认记录"
            )
        confirmed = ConfirmedBriefInput(
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            brief_version_id=row["brief_version_id"],
            confirmation_id=row["confirmation_id"],
            brief_snapshot_id=row["brief_snapshot_id"],
            payload=ArtifactPayload(row["payload_ref"], row["payload_sha256"]),
            confirmation_status=row["confirmation_status"],
            unresolved_conflict_count=row["unresolved_conflict_count"],
            confirmed_by_actor_id=row["confirmed_by_actor_id"],
            confirmed_at=row["confirmed_at"],
        )
        try:
            self.services.briefs.resolve_confirmed(
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                confirmed=confirmed,
            )
        except Exception as exc:
            raise self._as_blocked(exc) from exc
        return confirmed

    def _require_current_plan_pair(
        self, auth_token: str, event: dict, paired: PairedConfirmedBriefCreativeStandardInput,
    ) -> tuple:
        """Distinguish an intact historical binding from forged/missing evidence.

        Input changes never rewrite the waiting task. Validate its frozen evidence
        before reporting it as stale, so corruption is not a recovery shortcut.
        """
        brief = paired.brief
        identity = {"action": "prepare_plan", "workspace_id": brief.workspace_id,
                    "project_id": brief.project_id}
        if any(event.get(key) != value for key, value in identity.items()):
            raise FormalGenerationBlocked("PLAN_EVENT_INVALID", "规划 task 身份绑定不一致")
        expected = {
            "brief_version_id": brief.brief_version_id, "confirmation_id": brief.confirmation_id,
            "brief_snapshot_id": brief.brief_snapshot_id, "brief_payload_ref": brief.payload.ref,
            "brief_payload_sha256": brief.payload.sha256, **self._paired_event_evidence(paired),
        }
        if any(not str(event.get(key) or "").strip() for key in
               (*expected, "plan_ref", "plan_sha256", "manifest_ref", "manifest_sha256")):
            raise FormalGenerationBlocked("PLAN_EVENT_INVALID", "规划 task 证据绑定不一致")
        try:
            run_id = self._run_id_from_ref(event["plan_ref"])
            manifest = self.services.artifacts.resolve_manifest(run_id, expected_sha256=event["manifest_sha256"])
            artifact = self.services.artifacts.resolve_stage_artifact(run_id, "script_plan",
                expected_manifest_sha256=manifest.sha256)
            if manifest.ref != event["manifest_ref"] or artifact.ref != event["plan_ref"] or artifact.sha256 != event["plan_sha256"]:
                raise FormalGenerationBlocked("PLAN_ARTIFACT_DRIFT", "规划产物与不可变事件不一致")
            raw = json.loads(artifact.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or any(not isinstance(raw.get(key), dict) for key in
                    ("confirmed_brief_input", "confirmed_creative_standard_provenance", "constraints")):
                raise ValueError("规划证据结构无效")
            if sha256_file(artifact.path) != artifact.sha256:
                raise FormalGenerationBlocked("PLAN_ARTIFACT_DRIFT", "规划产物读取期间已变化")
        except ArtifactResolutionError as exc:
            raise self._artifact_block(exc) from exc
        except (OSError, ValueError, TypeError) as exc:
            raise FormalGenerationBlocked("PLAN_ARTIFACT_UNAVAILABLE", "规划产物无法读取") from exc
        frozen = raw.get("confirmed_brief_input", {})
        standard = raw.get("confirmed_creative_standard_provenance", {})
        frozen_fields = {
            **{key: frozen.get(key) for key in ("workspace_id", "project_id", "brief_version_id", "confirmation_id", "brief_snapshot_id")},
            "brief_payload_ref": frozen.get("payload", {}).get("ref"),
            "brief_payload_sha256": frozen.get("payload", {}).get("sha256"),
            "creative_standard_version_id": standard.get("creative_standard_version_id"),
            "creative_standard_confirmation_id": standard.get("confirmation_id"),
            "creative_standard_brief_version_id": standard.get("brief_version_id"),
            "creative_standard_brief_snapshot_id": standard.get("brief_snapshot_id"),
            "creative_standard_brief_payload_ref": standard.get("brief_payload", {}).get("ref"),
            "creative_standard_brief_payload_sha256": standard.get("brief_payload", {}).get("sha256"),
            "creative_standard_card_payload_ref": standard.get("card_payload", {}).get("ref"),
            "creative_standard_card_payload_sha256": standard.get("card_payload", {}).get("sha256"),
        }
        constraints = raw.get("constraints", {})
        if (any(event.get(key) != value for key, value in frozen_fields.items())
                or any(constraints.get(key) != event.get(key) for key in
                       ("creative_draft_binding", "formal_asset_package", "project_image_context", "effective_knowledge"))):
            raise FormalGenerationBlocked("PLAN_EVENT_INVALID", "规划产物与原始绑定不一致")
        original = self._current_confirmed_brief(auth_token, brief.workspace_id,
            brief.project_id, event["brief_snapshot_id"])
        if frozen != _confirmed_brief_evidence(original):
            raise FormalGenerationBlocked("PLAN_EVENT_INVALID", "规划与原始需求确认证据不一致")
        if any(event.get(key) != value for key, value in expected.items()):
            raise FormalGenerationBlocked("PLAN_INPUTS_STALE",
                "需求或创作标准已更新，旧规划与当前资料绑定不一致；历史已保留，请按当前资料重新规划。")
        return manifest, artifact, raw

    @staticmethod
    def _validate_plan_event(
        event: dict, paired: PairedConfirmedBriefCreativeStandardInput,
        image_evidence: dict, package_evidence: dict | None,
        effective_knowledge: dict,
    ) -> None:
        confirmed = paired.brief
        required = {
            "action": "prepare_plan",
            "workspace_id": confirmed.workspace_id,
            "project_id": confirmed.project_id,
            "brief_version_id": confirmed.brief_version_id,
            "confirmation_id": confirmed.confirmation_id,
            "brief_snapshot_id": confirmed.brief_snapshot_id,
            "brief_payload_ref": confirmed.payload.ref,
            "brief_payload_sha256": confirmed.payload.sha256,
        }
        if any(event.get(key) != value for key, value in required.items()):
            raise FormalGenerationBlocked(
                "PLAN_EVENT_INVALID", "规划 task 事件与当前正式 Brief 绑定不一致"
            )
        standard_required = FormalGenerationService._paired_event_evidence(paired)
        if any(event.get(key) != value for key, value in standard_required.items()):
            raise FormalGenerationBlocked(
                "PLAN_EVENT_INVALID", "规划 task 事件与当前创作标准卡绑定不一致"
            )
        if event.get("project_image_context") != image_evidence:
            raise FormalGenerationBlocked(
                "FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED",
                "规划 task 事件与当前项目图片分析绑定不一致",
            )
        if event.get("formal_asset_package") != package_evidence:
            raise FormalGenerationBlocked(
                "PLAN_INPUTS_STALE", "所选模板已更新，旧规划与当前模板绑定不一致；请重新规划。",
            )
        if (
            (event.get("effective_knowledge") or {}).get("snapshot_sha256")
            != effective_knowledge["snapshot_sha256"]
        ):
            raise FormalGenerationBlocked(
                "KNOWLEDGE_RULE_DRIFT", "已审核有效知识规则已变化，必须重新规划",
            )
        for key in ("plan_ref", "plan_sha256", "manifest_ref", "manifest_sha256"):
            if not str(event.get(key) or "").strip():
                raise FormalGenerationBlocked(
                    "PLAN_EVENT_INVALID", "规划 task 事件缺少不可变产物证据"
                )

    @staticmethod
    def _validate_plan_artifact_binding(
        raw: dict, paired: PairedConfirmedBriefCreativeStandardInput,
        image_evidence: dict, package_evidence: dict | None,
        effective_knowledge: dict,
    ) -> None:
        if not isinstance(raw, dict):
            raise FormalGenerationBlocked("PLAN_ARTIFACT_UNAVAILABLE", "规划 artifact 不是对象")
        standard = paired.creative_standard
        expected_standard = {
            "creative_standard_version_id": standard.creative_standard_version_id,
            "confirmation_id": standard.confirmation_id,
            "brief_version_id": standard.brief_version_id,
            "brief_snapshot_id": standard.brief_snapshot_id,
            "brief_payload": standard.brief_payload.as_mapping(),
            "card_payload": standard.card_payload.as_mapping(),
        }
        if raw.get("confirmed_creative_standard_provenance", {}) != expected_standard:
            raise FormalGenerationBlocked(
                "PLAN_EVENT_INVALID", "规划 artifact 与当前创作标准卡绑定不一致"
            )
        if raw.get("constraints", {}).get("project_image_context") != image_evidence:
            raise FormalGenerationBlocked(
                "FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED",
                "规划 artifact 与当前项目图片分析绑定不一致",
            )
        constraints = raw.get("constraints", {})
        if constraints.get("formal_asset_package") != package_evidence:
            raise FormalGenerationBlocked(
                "ASSET_PACKAGE_DRIFT", "当前 Formal 资产包与规划 artifact 绑定不一致",
            )
        if (
            (constraints.get("effective_knowledge") or {}).get("snapshot_sha256")
            != effective_knowledge["snapshot_sha256"]
        ):
            raise FormalGenerationBlocked(
                "KNOWLEDGE_RULE_DRIFT", "已审核有效知识规则已变化，必须重新规划",
            )

    @staticmethod
    def _artifact_block(exc: Exception) -> FormalGenerationBlocked:
        code = str(getattr(exc, "code", "PLAN_ARTIFACT_UNAVAILABLE"))
        return FormalGenerationBlocked(code, "规划 artifact 不可用或已漂移")

    def _project_at_lock(self, auth_token: str, workspace_id: str, project_id: str, expected: int) -> dict:
        project = self.services.projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise FormalGenerationBlocked("CROSS_PROJECT_DENIED", "project 不属于目标 workspace")
        if project["project_version"] != expected:
            raise FormalGenerationBlocked("PROJECT_VERSION_CONFLICT", "project lock 已变化，必须刷新后重试")
        return project

    def _require_no_unsaved_script_draft(self, project: dict) -> None:
        base_version_id = str(project.get("current_version_id") or "")
        if not base_version_id:
            return
        with self.services.projects.store.connection() as connection:
            try:
                require_no_unsaved_script_draft(
                    connection,
                    workspace_id=str(project["workspace_id"]),
                    project_id=str(project["project_id"]),
                    base_version_id=base_version_id,
                )
            except WorkspaceServiceError as exc:
                raise FormalGenerationBlocked(exc.code, exc.message) from exc

    def _new_task(
        self,
        auth_token: str,
        project_id: str,
        expected: int,
        paired: PairedConfirmedBriefCreativeStandardInput,
        *,
        operation_key: str | None = None,
    ) -> dict:
        session = self.services.tasks.create_agent_session(auth_token, project_id=project_id)
        payload = {
            "brief": _confirmed_brief_evidence(paired.brief),
            "creative_standard": paired.creative_standard.as_mapping(),
        }
        if operation_key:
            payload["operation_key"] = operation_key
        return self.services.tasks.create_task(
            auth_token,
            agent_session_id=session["agent_session_id"],
            task_type="formal_generation_plan",
            payload_sha256=sha256_json(payload),
            expected_project_version=expected,
        )

    def _resolve_current_paired(
        self, auth_token: str, workspace_id: str, project_id: str,
    ) -> PairedConfirmedBriefCreativeStandardInput:
        try:
            paired = self.services.briefs.resolve_current_paired(
                auth_token, workspace_id=workspace_id, project_id=project_id,
            )
        except Exception as exc:
            raise self._as_blocked(exc) from exc
        if not isinstance(paired, PairedConfirmedBriefCreativeStandardInput):
            raise FormalGenerationBlocked(
                "CREATIVE_STANDARD_UNAVAILABLE", "当前项目缺少可消费的已确认 Brief/创作标准卡组合"
            )
        return paired

    def _synchronize_and_resolve_images(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        paired: PairedConfirmedBriefCreativeStandardInput,
        expected_project_version: int,
    ) -> ProjectImagePlanningReadiness:
        images = self.services.images
        if not isinstance(images, ProjectImagePoolService):
            raise FormalGenerationBlocked(
                "FORMAL_IMAGE_AUTHORITY_UNAVAILABLE",
                "正式图片规划门未注入项目图片权威服务",
            )
        try:
            images.synchronize_confirmed_brief(
                auth_token, workspace_id=workspace_id, project_id=project_id,
                brief_version_id=paired.brief.brief_version_id,
                brief_snapshot_id=paired.brief.brief_snapshot_id,
                expected_project_lock_version=expected_project_version,
            )
            return self._resolve_images(
                auth_token, workspace_id=workspace_id, project_id=project_id,
                paired=paired, expected_project_version=expected_project_version,
            )
        except WorkspaceServiceError as exc:
            if str(exc.code).startswith("IMAGE_"):
                raise FormalGenerationBlocked(
                    "FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED",
                    "当前项目图片或图片分析证据已漂移，必须重新人工核验",
                ) from exc
            raise self._as_blocked(exc) from exc

    def _resolve_images(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        paired: PairedConfirmedBriefCreativeStandardInput,
        expected_project_version: int,
    ) -> ProjectImagePlanningReadiness:
        images = self.services.images
        if not isinstance(images, ProjectImagePoolService):
            raise FormalGenerationBlocked(
                "FORMAL_IMAGE_AUTHORITY_UNAVAILABLE",
                "正式图片规划门未注入项目图片权威服务",
            )
        try:
            return images.resolve_for_planning(
                auth_token, workspace_id=workspace_id, project_id=project_id,
                brief_version_id=paired.brief.brief_version_id,
                brief_snapshot_id=paired.brief.brief_snapshot_id,
                expected_project_lock_version=expected_project_version,
            )
        except WorkspaceServiceError as exc:
            if str(exc.code).startswith("IMAGE_"):
                raise FormalGenerationBlocked(
                    "FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED",
                    "当前项目图片或图片分析证据已漂移，必须重新人工核验",
                ) from exc
            raise self._as_blocked(exc) from exc

    @staticmethod
    def _image_context_images(images: ProjectImagePlanningReadiness) -> tuple:
        return tuple(
            image
            for image in images.images
            if (
                image.current_analysis is not None
                and image.current_analysis.status == "pass"
                and image.business_summary is not None
            )
        )

    @classmethod
    def _image_context_readiness(
        cls, images: ProjectImagePlanningReadiness,
    ) -> ProjectImagePlanningReadiness:
        return replace(
            images,
            passed=len(cls._image_context_images(images)),
            images=cls._image_context_images(images),
        )

    @classmethod
    def _require_images_ready(
        cls,
        images: ProjectImagePlanningReadiness,
        *,
        generation_mode: str | None = None,
    ) -> None:
        if not isinstance(images, ProjectImagePlanningReadiness):
            raise TypeError("正式图片规划门只接受 ProjectImagePoolService 返回对象")
        if cls._image_context_images(images):
            return
        if generation_mode == "text_only_draft":
            return
        if images.total == 0:
            raise FormalGenerationBlocked(
                "IMAGE_STRATEGY_CONFIRMATION_REQUIRED",
                "图片模式需要先上传或采用类别图片",
            )
        raise FormalGenerationBlocked(
            "FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED",
            "当前项目没有可用于正式规划的已通过图片；请先完成图片分析或明确选择文字草稿",
        )

    @staticmethod
    def _image_event_evidence(images: ProjectImagePlanningReadiness) -> dict:
        if not isinstance(images, ProjectImagePlanningReadiness):
            raise TypeError("项目图片规划证据只接受 ProjectImagePlanningReadiness")
        context_images = FormalGenerationService._image_context_images(images)
        return {
            "workspace_id": images.workspace_id,
            "project_id": images.project_id,
            "brief_version_id": images.brief_version_id,
            "brief_snapshot_id": images.brief_snapshot_id,
            "total": images.total,
            "passed": len(context_images),
            "images": [
                {
                    "image_id": image.image_id,
                    "image_sha256": image.image_sha256,
                    "analysis_ref": image.current_analysis.evidence.ref,
                    "analysis_sha256": image.current_analysis.evidence.sha256,
                    "business_summary": {
                        "caption": image.business_summary.caption,
                        "tags": list(image.business_summary.tags),
                        "subjects": list(image.business_summary.subjects),
                        "scene": image.business_summary.scene,
                        "purpose": image.business_summary.purpose,
                        "quality": image.business_summary.quality,
                        "brand": image.business_summary.brand,
                        "candidate_only": True,
                    },
                }
                for image in context_images
            ],
        }

    @staticmethod
    def _paired_event_evidence(paired: PairedConfirmedBriefCreativeStandardInput) -> dict:
        standard = paired.creative_standard
        return {
            "creative_standard_version_id": standard.creative_standard_version_id,
            "creative_standard_confirmation_id": standard.confirmation_id,
            "creative_standard_brief_version_id": standard.brief_version_id,
            "creative_standard_brief_snapshot_id": standard.brief_snapshot_id,
            "creative_standard_brief_payload_ref": standard.brief_payload.ref,
            "creative_standard_brief_payload_sha256": standard.brief_payload.sha256,
            "creative_standard_card_payload_ref": standard.card_payload.ref,
            "creative_standard_card_payload_sha256": standard.card_payload.sha256,
        }

    def _require_same_current_pair(
        self,
        auth_token: str,
        workspace_id: str,
        project_id: str,
        expected: PairedConfirmedBriefCreativeStandardInput,
    ) -> None:
        if self._resolve_current_paired(auth_token, workspace_id, project_id) != expected:
            raise FormalGenerationBlocked(
                "CREATIVE_STANDARD_DRIFT", "规划期间当前 Brief/创作标准卡绑定已变化"
            )

    def _reject_noncurrent_image(self, image_id: str) -> None:
        images = self.services.images
        if not isinstance(images, ProjectImagePoolService):
            raise FormalGenerationBlocked("FORMAL_IMAGE_AUTHORITY_UNAVAILABLE", "正式图片规划门未注入项目图片权威服务")
        with images.store.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM project_images WHERE project_image_id=?", (image_id,),
            ).fetchone()
        if exists is not None:
            raise FormalGenerationBlocked("CROSS_PROJECT_DENIED", "目标图片不属于当前项目")
        raise FormalGenerationBlocked("NOT_FOUND", "当前确认 Brief 中不存在目标项目图片")

    def _require_same_images(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        paired: PairedConfirmedBriefCreativeStandardInput,
        expected_project_version: int,
        expected: dict,
        generation_mode: str | None = None,
    ) -> None:
        images = self._resolve_images(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            paired=paired, expected_project_version=expected_project_version,
        )
        self._require_images_ready(images, generation_mode=generation_mode)
        if self._image_event_evidence(images) != expected:
            raise FormalGenerationBlocked(
                "FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED",
                "规划期间当前项目图片分析绑定已变化",
            )

    def _plan_event(self, auth_token: str, task_id: str) -> dict:
        events = self.services.tasks.list_task_events(auth_token, task_id)
        for event in reversed(events):
            if event["status"] == "waiting_user" and event["details"].get("action") == "prepare_plan":
                return event["details"]
        raise FormalGenerationBlocked("PLAN_CONFIRMATION_REQUIRED", "未找到可确认的规划事件")

    def _plan_dto(self, task: dict, plan, artifact, manifest, bound: dict) -> dict:
        def public_evidence_refs(values: list[dict]) -> list[dict]:
            result = []
            for raw in values:
                item = dict(raw or {})
                source_raw = dict(item.get("source") or item.get("source_ref") or {})
                source = {
                    key: source_raw[key]
                    for key in ("page", "sheet", "row", "column", "slide", "paragraph", "image_index")
                    if key in source_raw
                }
                for key in ("sheet", "row"):
                    if source_raw.get("source_" + key) is not None:
                        source.setdefault(key, source_raw["source_" + key])
                source_file = str(source_raw.get("file") or source_raw.get("source_file") or "")
                if source_file and not source_file.startswith("artifact://"):
                    source["file"] = Path(source_file).name
                evidence_text = str(item.get("text") or "")
                if any(secret in evidence_text.casefold() for secret in ("artifact://", "sha256", "content_sha")):
                    evidence_text = ""
                result.append({
                    key: value
                    for key, value in {
                        "type": item.get("type"),
                        "field": item.get("field"),
                        "fact_id": item.get("fact_id"),
                        "text": evidence_text,
                        "source": source or None,
                    }.items()
                    if value not in (None, "", {})
                })
            return result

        def public_field_decisions(values: list[dict]) -> list[dict]:
            result = []
            for raw in values:
                item = dict(raw or {})
                item["evidence_refs"] = public_evidence_refs(
                    list(item.get("evidence_refs") or [])
                )
                item["candidates"] = [
                    {
                        **{
                            key: value for key, value in dict(candidate).items()
                            if key != "evidence_refs"
                        },
                        "evidence_refs": public_evidence_refs(
                            list(dict(candidate).get("evidence_refs") or [])
                        ),
                    }
                    for candidate in item.get("candidates") or []
                ]
                result.append(item)
            return result

        default_brief_source = next((
            dict((event.get("brief_evidence") or {}).get("source") or {})
            for event in plan.events
            if dict((event.get("brief_evidence") or {}).get("source") or {})
        ), {})
        shots = []
        for index, (script, detail, planned) in enumerate(zip(plan.script_rows, plan.detail_rows, plan.shot_plans), start=1):
            brief_source = (
                dict((planned.get("brief_evidence") or {}).get("source") or {})
                or default_brief_source
            )
            project_brief_source = dict(planned.get("brief_source") or {})
            source = {
                key: brief_source[key]
                for key in ("file", "page", "sheet", "row", "column", "slide", "paragraph", "image_index")
                if key in brief_source
            } or ({
                "file": Path(str(
                    project_brief_source.get("source_file")
                    or project_brief_source.get("file") or ""
                )).name,
                "sheet": project_brief_source.get("source_sheet")
                or project_brief_source.get("sheet"),
                "row": project_brief_source.get("source_row")
                or project_brief_source.get("row"),
            } if project_brief_source else {})
            source = {key: value for key, value in source.items() if value not in (None, "")}
            source = source or {
                "file": detail.get("采用来源文件"),
                "sheet": detail.get("采用来源 sheet"),
                "row": detail.get("采用来源行"),
            }
            template = dict(planned.get("template_source") or source)
            visual_deferred = not planned.get("creative_block") and any(
                field.get("field") == "visual_description" and field.get("value_status") == "deferred"
                for field in planned.get("field_decisions") or [])
            shots.append({
                "sequence": index,
                "shot_no": str(script.get("镜号") or index),
                "primary_event": str(planned.get("primary_event") or ""),
                "events": list(planned.get("events") or []),
                "requirement_ids": list(planned.get("requirement_ids") or []),
                "structure": str(script.get("结构") or planned.get("structure") or ""),
                "structure_source": str(planned.get("structure_source") or ""),
                "position_rule": dict(planned.get("position_rule") or {}),
                "field_decisions": public_field_decisions(
                    list(planned.get("field_decisions") or [])
                ),
                "template_source": {
                    "file": Path(str(
                        template.get("source_file") or template.get("file") or ""
                    )).name,
                    "sheet": template.get("source_sheet") or template.get("sheet"),
                    "row": template.get("source_row") or template.get("row"),
                    "structure_source": {
                        "file": Path(str(
                            template.get("structure_source_file")
                            or template.get("source_file") or ""
                        )).name,
                        "sheet": template.get("structure_source_sheet")
                        or template.get("source_sheet"),
                        "row": template.get("structure_source_row")
                        or template.get("source_row"),
                    },
                },
                "visual": "" if visual_deferred else str(script.get("画面描述") or ""),
                "visual_status": "pending_draft" if visual_deferred else "available",
                **({"structural_adaptation": str(planned["structural_assignment"].get("reason") or "")}
                   if planned.get("structural_assignment") else {}),
                "sound": str(script.get("声音") or ""),
                "sound_status": "template_reference" if not planned.get("creative_block") and any(
                    field.get("field") == "sound" and field.get("selected_source") == "template_rule"
                    for field in planned.get("field_decisions") or []) else "available",
                "duration_seconds": script.get("时长(秒)"),
                **({"creative_order_note": str(planned["creative_order_note"])}
                   if planned.get("creative_order_note") else {}),
                **({"creative_block": {
                    "block_id": planned["creative_block"]["block_id"],
                    "section": planned["creative_block"]["fields"]["段落"],
                    "original_duration": planned["creative_block"]["fields"]["时长"],
                    "text_preserved": True,
                    "assistant_protected": bool(planned["creative_block"].get("assistant_protected")),
                }} if planned.get("creative_block") else {}),
                "source": source,
            })
        audit = dict(plan.planning_audit)
        original_segments = []
        rule_payload = dict(plan.template_rule_contract.get("payload") or {})
        for rule in rule_payload.get("rules") or []:
            if rule.get("field") != "structure":
                continue
            selector = rule.get("selector") or {}
            original_segments.append({"structure": str(rule.get("value") or ""),
                                      "shot_count": int(selector.get("slot_end") or 0) - int(selector.get("slot_start") or 0) + 1})
        current_segments = []
        for shot in shots:
            if current_segments and current_segments[-1]["structure"] == shot["structure"]:
                current_segments[-1]["shot_count"] += 1
            else:
                current_segments.append({"structure": shot["structure"], "shot_count": 1})
        frozen_package = dict(plan.constraints.get("formal_asset_package") or {})
        template_file = next((str(item.get("模板来源文件") or "") for item in plan.detail_rows
                              if item.get("模板来源文件")), "")
        template_summary = {
            "label": Path(template_file).stem or str(plan.brief.get("脚本类型") or "") + "模板",
            "package_version": frozen_package.get("package_version"),
            "selection_mode": "explicit" if frozen_package.get("alias") == "project" else "default",
            "original_segments": original_segments,
            "current_segments": current_segments,
            "structure_change_count": sum(shot["structure_source"] == "brief_confirmed_override" for shot in shots),
            "position_requirement_count": sum(shot["position_rule"].get("type") not in {None, "", "none"} for shot in shots),
        }
        if frozen_package and self.template_packages is not None:
            record = self.template_packages.registry.resolve_evidence(
                category_id_for_script_type(plan.brief["脚本类型"]), frozen_package)
            template_summary["sources"] = self.template_packages.describe_sources(record,
                frozen_bindings=frozen_package.get("library_bindings", []))
            template_summary["sources"]["warnings"].extend(frozen_package.get("library_warnings", []))
        field_labels = {"structure": "段落", "camera_type": "机位", "shot_size": "景别", "camera_move": "运镜",
                        "sound": "声音", "duration_seconds": "时长", "visual_description": "画面"}
        adjustments = []
        for shot in shots:
            for choice in shot["field_decisions"]:
                if choice.get("selected_source") != "brief" or choice.get("field") not in field_labels:
                    continue
                previous = next((c.get("value") for c in choice.get("candidates") or [] if c.get("source") == "template_rule"), None)
                final = choice.get("final_value")
                if previous == final or final in (None, "") or not isinstance(final, (str, int, float)):
                    continue
                label = field_labels[choice["field"]]
                adjustments.append({"kind": "structure" if choice["field"] == "structure" else "brief",
                    "shot_no": shot["shot_no"], "field": choice["field"], "before": previous, "after": final,
                    "summary": f"第 {shot['shot_no']} 镜{label}：{previous or '模板默认'} → {final}"})
            if shot["position_rule"].get("type") not in {None, "", "none"}:
                content = shot["primary_event"] or shot["structure"]
                adjustments.append({"kind": "position", "shot_no": shot["shot_no"], "field": "position_rule",
                    "summary": f"第 {shot['shot_no']} 镜：{content}"})
        original_count = sum(item["shot_count"] for item in original_segments)
        if original_count and original_count != len(shots):
            adjustments.append({"kind": "shot_count", "before": original_count, "after": len(shots),
                "summary": f"镜数：模板 {original_count} 镜 → 本次 {len(shots)} 镜"})
        template_summary["adjustments"] = adjustments
        semantic_payload = json.loads(Path(plan.path).read_text(encoding='utf-8'))
        planning_semantics = semantic_payload.get('planning_semantics')
        if planning_semantics:
            template_summary['planning_semantics'] = {key: planning_semantics.get(key) for key in ('status', 'message')}
            if planning_semantics.get('status') != 'ready' and template_summary.get('sources'):
                template_summary['sources']['warnings'].append(planning_semantics.get('message', ''))
        semantic_matching = semantic_payload.get('semantic_matching')
        if semantic_matching:
            template_summary['semantic_matching'] = {key: semantic_matching.get(key) for key in
                ('status', 'message', 'matches')}
            if template_summary.get('sources'):
                template_summary['sources']['warnings'].append(semantic_matching.get('message', ''))
        template_summary["structure_note"] = "已按确认需求调整段落" if any(item["kind"] == "structure" for item in adjustments) else "沿用模板段落"
        return {
            "template_summary": template_summary,
            **({"creative_conversion": {
                "block_count": len(plan.constraints["creative_draft_binding"]["blocks"]),
                "mapped_count": sum(bool(shot.get("creative_block")) for shot in shots),
                "added_shot_count": sum(not shot.get("creative_block") for shot in shots),
                "protected_count": sum(bool(shot.get("creative_block", {}).get("assistant_protected")) for shot in shots),
                "mappings": [{"block_id": shot["creative_block"]["block_id"], "shot_no": shot["shot_no"],
                    "section": shot["creative_block"]["section"], "before_duration": shot["creative_block"]["original_duration"],
                    "after_duration_seconds": shot["duration_seconds"], "visual_preserved": True, "sound_preserved": True,
                    "assistant_protected": shot["creative_block"]["assistant_protected"]}
                    for shot in shots if shot.get("creative_block")],
                "notice": "初稿画面与声音原文保留；新增镜头及规划时长请在下方核对。事实与人工质量仍按原流程确认。",
            }} if plan.constraints.get("creative_draft_binding") else {}),
            "plan_id": task["task_id"],
            "plan_sha256": artifact.sha256,
            "status": "waiting_user",
            "events": [
                {
                    "sequence": index,
                    "event_id": str(item.get("event_id") or ""),
                    "text": str(item.get("text") or item.get("title") or item.get("event") or ""),
                    "kind": str(item.get("kind") or "flow_event"),
                    "evidence_refs": public_evidence_refs(list(item.get("evidence_refs") or [])),
                    "event": str(item.get("text") or item.get("title") or item.get("event") or ""),
                }
                for index, item in enumerate(plan.events, start=1)
            ],
            "requirements": [
                {
                    **{key: value for key, value in item.items() if key != "evidence_refs"},
                    "evidence_refs": public_evidence_refs(list(item.get("evidence_refs") or [])),
                }
                for item in plan.requirements
            ],
            "coverage": [
                {
                    **{key: value for key, value in item.items() if key != "evidence_refs"},
                    "evidence_refs": public_evidence_refs(list(item.get("evidence_refs") or [])),
                }
                for item in plan.coverage
            ],
            "capacity_report": dict(plan.capacity_report),
            "traceability": [
                {
                    **item,
                    "template_source": [
                        {
                            **{
                                key: value for key, value in dict(source).items()
                                if key not in {
                                    "source_file", "file", "structure_source_file",
                                    "structure_source_sheet", "structure_source_row",
                                }
                            },
                            **(
                                {"file": Path(str(source.get("source_file") or source.get("file"))).name}
                                if source.get("source_file") or source.get("file") else {}
                            ),
                            "structure_source": {
                                "file": Path(str(
                                    source.get("structure_source_file")
                                    or source.get("source_file") or source.get("file") or ""
                                )).name,
                                "sheet": source.get("structure_source_sheet")
                                or source.get("source_sheet") or source.get("sheet"),
                                "row": source.get("structure_source_row")
                                or source.get("source_row") or source.get("row"),
                            },
                        }
                        for source in item.get("template_source") or []
                    ],
                }
                for item in plan.traceability
            ],
            "template_rule_contract": {
                key: value
                for key, value in dict(plan.template_rule_contract).items()
                if key not in {"path", "payload", "sha256"}
            },
            "conflicts": [
                {
                    **{
                        key: value for key, value in item.items()
                        if key != "evidence_refs"
                    },
                    "evidence_refs": public_evidence_refs(
                        list(item.get("evidence_refs") or [])
                    ),
                }
                for item in plan.conflicts
            ],
            "decision_counts": dict(plan.decision_counts),
            "shot_count": len(shots),
            "total_duration_seconds": sum(int(item["duration_seconds"]) for item in shots),
            "shots": shots,
            "audit": {
                "status": str(audit.get("status") or ""),
                "blocking_codes": list(audit.get("blocking_codes") or []),
                "clarification_codes": list(audit.get("clarification_codes") or []),
                "warning_codes": list(audit.get("warning_codes") or []),
                "findings": list(audit.get("findings") or []),
            },
            "needs_clarification": audit.get("status") == "needs_clarification",
        }

    @staticmethod
    def _require_plan_display_complete(plan) -> None:
        """A preview is not usable when its promised per-shot fields are blank."""
        for index, (script, detail, planned) in enumerate(
            zip(plan.script_rows, plan.detail_rows, plan.shot_plans), start=1
        ):
            required = {
                "主事件": planned.get("primary_event"),
                "画面": script.get("画面描述"),
                "声音": script.get("声音"),
                "时长": script.get("时长(秒)"),
                "来源文件": detail.get("采用来源文件"),
                "来源Sheet": detail.get("采用来源 sheet"),
                "来源行": detail.get("采用来源行"),
            }
            missing = [name for name, value in required.items() if not str(value or "").strip()]
            if missing:
                raise FormalGenerationBlocked(
                    "PLAN_DTO_INCOMPLETE",
                    f"第 {index} 镜规划缺少页面必需字段: {'、'.join(missing)}",
                )

    def _block_task(self, auth_token: str, task: dict, code: str, details: dict) -> None:
        try:
            self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
                status="blocked", error_code=code,
                details={
                    **details,
                    "provider_calls": details.get("provider_calls", 0),
                    "script_draft_attempt": details.get("script_draft_attempt", 0),
                    "version_created": details.get("version_created", 0),
                    "p00_attempt": details.get("p00_attempt", 0),
                },
            )
        except WorkspaceServiceError as exc:
            if exc.code != "INVALID_TASK_TRANSITION":
                raise

    def _project_cache(self, project_id: str) -> Path:
        return self.runs_root / project_id

    def _source_root_for(
        self, script_type: str, *, asset_package: Path | None = None,
    ) -> Path:
        if asset_package is not None:
            return Path(load_asset_package(asset_package)["source_workbook_path"]).resolve()
        if self.source_root_resolver is None:
            return self.source_root
        return Path(self.source_root_resolver(script_type)).expanduser().resolve()

    def _asset_package_for(self, script_type: str, *, project: dict | None = None,
                           template_ref: str | None = None,
                           frozen_evidence: dict | None = None) -> tuple[Path | None, dict | None]:
        project = project or {}
        ref = template_ref or project.get("template_package_ref")
        if self.asset_package_resolver is None and not ref:
            return None, None
        try:
            if ref:
                value = self._template_service().resolve_template(category_id_for_script_type(script_type), str(ref))
                if not template_ref and value["package_sha256"] != project.get("template_package_sha256"):
                    raise ValueError("项目绑定的模板内容已变化")
                value = {**value, "alias": "project"}
            else:
                value = self.asset_package_resolver(script_type)
        except (PackageRegistryError, PackagePublicationError, OSError, ValueError) as exc:
            raise FormalGenerationBlocked(
                "ASSET_PACKAGE_DRIFT", f"Formal 资产包解析失败: {exc}",
            ) from exc
        if not value:
            return None, None
        declared = dict(value) if isinstance(value, Mapping) else {}
        raw_path = declared.get("local_path") if declared else value
        if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
            raise FormalGenerationBlocked(
                "ASSET_PACKAGE_DRIFT", "Formal 资产包权威解析记录缺少路径",
            )
        path = Path(raw_path).expanduser().resolve()
        if declared:
            required = {
                "package_id": declared.get("package_id"),
                "package_version": declared.get("package_version"),
                "package_sha256": declared.get("package_sha256"),
                "local_path": declared.get("local_path"),
                "resolved_ref": declared.get("resolved_ref"),
                "alias": declared.get("alias"),
            }
            if (
                any(value is None or value == "" for value in required.values())
                or type(required["package_version"]) is not int
                or not path.is_dir()
            ):
                raise FormalGenerationBlocked(
                    "ASSET_PACKAGE_DRIFT", "Formal 资产包权威解析记录不完整",
                )
            current = declared
        else:
            current = self._legacy_asset_package_cache.get(path)
            if current is None:
                try:
                    current = build_package_record(path)
                except PackageRegistryError as exc:
                    raise FormalGenerationBlocked(
                        "ASSET_PACKAGE_DRIFT",
                        f"Formal 资产包不可用或内容已漂移: {exc}",
                    ) from exc
                self._legacy_asset_package_cache[path] = current
        evidence = {
            "package_id": current["package_id"],
            "package_version": current["package_version"],
            "package_sha256": current["package_sha256"],
            "alias": str(declared.get("alias") or "legacy-configured"),
            "resolved_ref": str(
                declared.get("resolved_ref")
                or f"{current['package_id']}@{current['package_version']}"
            ),
        }
        if frozen_evidence is None or "source_semantics" in frozen_evidence:
            semantics = load_asset_package(path)["source_semantics"]
            if semantics.get("status") == "recorded":
                evidence["source_semantics"] = semantics
        if self.template_packages is not None and (frozen_evidence is None or "library_bindings" in frozen_evidence):
            records, warnings = self.template_packages.library_records(current,
                frozen_bindings=frozen_evidence.get("library_bindings", []) if frozen_evidence is not None else None)
            evidence.update(category_id=category_id_for_script_type(script_type),
                library_bindings=[{key: record[key] for key in
                    ("category_id", "package_id", "package_version", "package_sha256", "resolved_ref")} for record in records],
                library_warnings=list(frozen_evidence.get("library_warnings", [])) if frozen_evidence is not None else warnings)
        return path, evidence

    def _library_paths(self, script_type: str, evidence: dict | None) -> list[Path]:
        bindings = (evidence or {}).get("library_bindings", [])
        if not bindings:
            return []
        return [Path(self._template_service().registry.resolve_evidence(
            binding.get("category_id") or category_id_for_script_type(script_type), binding)["local_path"]) for binding in bindings]

    def _creative_basis(self, auth_token: str, project: dict, brief: dict) -> dict:
        """Freeze the same authorities before the initial draft's provider call."""
        from .asset_package import package_script_catalog
        from .script_type_adapter import metadata_for_category_id
        from .script_draft import frozen_creative_context
        category = project.get("category_id")
        package, rules, examples = None, {}, []
        if category:
            path, package = self._asset_package_for(metadata_for_category_id(category).display_name, project=project)
            if path:
                loaded = load_asset_package(path)
                rules = loaded["template_rule_contract"]
                template, _, _ = package_script_catalog(path, expected_category_id=category)
                examples = [{key: getattr(shot, key) for key in (
                    "slot_index", "structure", "generic_desc", "visual_desc", "shot_function",
                    "sound", "brand_judgement_original", "source_brand", "source_file", "source_sheet", "source_row")}
                    for shot in template]
        constraints = {"formal_asset_package": package, "template_rule_contract": rules,
                       "effective_knowledge": self._effective_knowledge(auth_token, project["project_id"])}
        standard = brief.get("creative_standard") or brief.get("brief", {}).get("creative_standard") or {}
        basis = {"brief_version_id": brief.get("brief_version_id"), "category_id": category,
            "creative_standard": standard.get("content", {}), "creative_standard_status": standard.get("status", "not_recorded"),
            "formal_asset_package": package, "template_examples": examples,
            **frozen_creative_context(constraints, rules)}
        return {**basis, "snapshot_sha256": sha256_json(basis)}

    def _effective_knowledge(self, auth_token: str, project_id: str) -> dict:
        authority = self.knowledge.list_effective_rules(auth_token, project_id)
        rules = [
            {
                "rule_id": str(rule["rule_id"]),
                "scope": str(rule["scope"]),
                "statement": str(rule["statement"]),
                "statement_sha256": str(rule["statement_sha256"]),
                "evidence_refs": list(rule.get("evidence_refs") or []),
                "active_version": int(rule["active_version"]),
            }
            for rule in authority.get("rules") or []
        ]
        content = {
            "schema_version": "mplus-effective-knowledge/v1",
            "rules": rules,
        }
        return {
            **content,
            "knowledge_version": int(authority.get("knowledge_version") or 0),
            "snapshot_sha256": sha256_json(content),
            "pending_candidates_included": False,
        }

    def _require_same_generation_inputs(
        self,
        auth_token: str,
        *,
        project_id: str,
        script_type: str,
        expected_package: dict | None,
        expected_knowledge: dict,
        template_ref: str | None = None,
    ) -> None:
        project = self.services.projects.get_project(auth_token, project_id)
        _, current_package = self._asset_package_for(script_type, project=project, template_ref=template_ref,
            frozen_evidence=expected_package)
        if current_package != expected_package:
            raise FormalGenerationBlocked(
                "ASSET_PACKAGE_DRIFT", "Formal 资产包在运行期间已变化",
            )
        current_knowledge = self._effective_knowledge(auth_token, project_id)
        if current_knowledge["snapshot_sha256"] != expected_knowledge["snapshot_sha256"]:
            raise FormalGenerationBlocked(
                "KNOWLEDGE_RULE_DRIFT", "已审核有效知识规则在运行期间已变化",
            )

    @staticmethod
    def _run_id_from_ref(ref: str) -> str:
        prefix = "artifact://run/"
        suffix = "/script_plan"
        if not isinstance(ref, str) or not ref.startswith(prefix) or not ref.endswith(suffix):
            raise FormalGenerationBlocked("PLAN_CONFIRMATION_MISMATCH", "规划 artifact ref 无效")
        return ref[len(prefix):-len(suffix)]

    @staticmethod
    def _error_code(exc: Exception) -> str:
        return str(getattr(exc, "code", "FORMAL_GENERATION_BLOCKED"))

    @staticmethod
    def _as_blocked(exc: Exception) -> FormalGenerationBlocked:
        if isinstance(exc, FormalGenerationBlocked):
            return exc
        message = str(getattr(exc, "message", exc) or "正式生成已阻断")
        if message.startswith("不支持的脚本类型:"):
            return FormalGenerationBlocked("SCRIPT_TYPE_UNSUPPORTED", message)
        if "视频时长必须是正整数秒" in message:
            return FormalGenerationBlocked("BRIEF_DURATION_INVALID", message)
        code = str(getattr(exc, "code", "FORMAL_GENERATION_BLOCKED"))
        return FormalGenerationBlocked(code, message)

    def completed_plan_result(self, auth_token: str, *, workspace_id: str,
                              project_id: str, version_id: str, completed_after: str) -> dict | None:
        """Read a durable generation receipt, never infer one from a saved edit."""
        project = self.services.projects.get_project(auth_token, project_id)
        if project['workspace_id'] != workspace_id:
            raise FormalGenerationBlocked('CROSS_PROJECT_DENIED', '规划结果不属于当前工作区')
        from .identity_service import require_permission
        with self.services.projects.store.connection() as connection:
            actor = require_permission(connection, auth_token, workspace_id=workspace_id,
                                       project_id=project_id, permission='task.read')
            rows = connection.execute("""SELECT t.task_id, e.details_json, v.run_id,
                v.artifact_ref, v.content_sha256 FROM tasks t
                JOIN task_events e ON e.task_id=t.task_id
                JOIN project_versions v ON v.version_id=json_extract(e.details_json, '$.version_id')
                  AND v.project_id=t.project_id
                WHERE t.project_id=? AND t.actor_id=? AND t.task_type='formal_generation_plan'
                  AND t.status='completed' AND e.status='completed' AND v.version_id=?
                  AND e.created_at>=? AND v.created_at>=?
                  AND json_extract(e.details_json, '$.action')='generate_from_confirmed_plan'
                ORDER BY e.created_at DESC, e.sequence_no DESC""",
                (project_id, actor['actor_id'], version_id, completed_after, completed_after)).fetchall()
        for row in rows:
            self._require_plan_task(auth_token, project_id, row['task_id'])
            plan = self._plan_event(auth_token, row['task_id'])
            receipt = json.loads(row['details_json'])
            manifest = self.services.artifacts.resolve_manifest(row['run_id'])
            workbook = self.services.artifacts.resolve_artifact(row['run_id'], 'workbook')
            artifact = self.services.artifacts.resolve_stage_artifact(
                self._run_id_from_ref(plan['plan_ref']), 'script_plan')
            if (any(receipt.get(key) != plan.get(key) for key in ('plan_ref', 'plan_sha256'))
                    or artifact.sha256 != plan['plan_sha256'] or receipt.get('run_id') != row['run_id']
                    or (receipt.get('manifest_ref'), receipt.get('manifest_sha256')) != (manifest.ref, manifest.sha256)
                    or (row['artifact_ref'], row['content_sha256']) != (workbook.ref, workbook.sha256)):
                raise FormalGenerationBlocked('PLAN_ARTIFACT_DRIFT', '规划生成回执与冻结版本不一致，请核对原任务')
            return {'status': 'completed', 'plan_id': row['task_id'], 'version_id': version_id,
                    'plan_sha256': plan['plan_sha256'],
                    'brief_version_id': receipt.get('creative_standard_brief_version_id')}
        # Successful retries preserve the original failed task. Their authority
        # is the executor checkpoint and the generated run's explicit attempt.
        version = self.services.projects.get_project_version(auth_token, version_id)
        if version['project_id'] != project_id or version['created_at'] < completed_after:
            return None
        manifest = self.services.artifacts.read_manifest(version['run_id'])
        attempt_id = (manifest.get('run_provenance') or {}).get('recovery_attempt_id')
        if not attempt_id:
            return None
        with self.services.projects.store.connection() as connection:
            recovery = connection.execute("""SELECT p.task_id FROM recovery_executions e
                JOIN recovery_proposals p ON p.recovery_proposal_id=e.recovery_proposal_id
                WHERE e.new_attempt_id=? AND p.project_id=? AND p.workspace_id=?
                  AND e.confirmed_by_actor_id=? AND p.requested_stage_id='formal_generation_plan'""",
                (attempt_id, project_id, workspace_id, actor['actor_id'])).fetchone()
        if recovery is None:
            return None
        from .formal_recovery_executor import FormalRecoveryExecutor
        terminal = FormalRecoveryExecutor(self.services.projects.store, handlers={}).terminal_for_attempt(attempt_id)
        workbook = self.services.artifacts.resolve_artifact(version['run_id'], 'workbook')
        if (not terminal or terminal['status'] != 'succeeded'
                or terminal['stage_id'] != 'formal_generation_plan' or terminal['created_at'] < completed_after
                or terminal['output_ref'] != workbook.ref):
            return None
        self._require_plan_task(auth_token, project_id, recovery['task_id'])
        plan = self._plan_event(auth_token, recovery['task_id'])
        artifact = self.services.artifacts.resolve_stage_artifact(
            self._run_id_from_ref(plan['plan_ref']), 'script_plan')
        if ((terminal['input_ref'], terminal['input_sha256']) != (plan['plan_ref'], plan['plan_sha256'])
                or artifact.sha256 != plan['plan_sha256']
                or (terminal['output_ref'], terminal['output_sha256']) != (workbook.ref, workbook.sha256)
                or (version['artifact_ref'], version['content_sha256']) != (workbook.ref, workbook.sha256)):
            raise FormalGenerationBlocked('PLAN_ARTIFACT_DRIFT', '恢复回执与冻结规划或生成版本不一致，请核对原任务')
        return {'status': 'completed', 'plan_id': recovery['task_id'], 'version_id': version_id,
                'plan_sha256': plan['plan_sha256'], 'recovery_attempt_id': attempt_id,
                'brief_version_id': plan.get('creative_standard_brief_version_id')}

    def execute_image_analysis_batch(self, auth_token: str, *, task_id: str) -> dict:
        from .project_image_analysis_batch import execute
        return execute(self, auth_token, task_id=task_id)

    def pause_image_analysis_batch(self, auth_token: str, *, task_id: str) -> dict:
        from .project_image_analysis_batch import pause
        return pause(self, auth_token, task_id=task_id)

    def read_image_analysis_batch(self, auth_token: str, **kwargs) -> dict:
        from .project_image_analysis_batch import read
        return read(self, auth_token, **kwargs)

    def read_template_method_context(self, auth_token: str, *, workspace_id: str,
                                     project_id: str) -> dict:
        """Read the selected template for discussion before any draft exists."""
        from .script_type_adapter import metadata_for_category_id
        from .script_draft import frozen_creative_context
        project = self._template_project(auth_token, workspace_id, project_id)
        if not project.get('category_id'):
            return {'template_semantics': {'status': 'not_recorded'}, 'template_rules': {}}
        script_type = metadata_for_category_id(project['category_id']).display_name
        path, package = self._asset_package_for(script_type, project=project)
        rules = load_asset_package(path)['template_rule_contract'] if path else {}
        context = frozen_creative_context({'formal_asset_package': package}, rules)
        return {**context, 'template_ref': (package or {}).get('resolved_ref', ''),
                'basis_kind': 'selected_template_reference'}

    def resume_image_analysis_batch(self, auth_token: str, *, task_id: str) -> dict:
        from .project_image_analysis_batch import resume
        return resume(self, auth_token, task_id=task_id)

    def start_image_analysis_batch(self, auth_token: str, **kwargs) -> dict:
        from .project_image_analysis_batch import start
        return start(self, auth_token, **kwargs)
