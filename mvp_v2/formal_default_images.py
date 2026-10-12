"""Preview-and-confirm adoption of verified category package images."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import stat
from threading import RLock
from types import SimpleNamespace
from typing import Callable

from PIL import Image

from .asset_package import AssetPackageError, load_asset_package
from .formal_image_workspace import (
    FormalImageWorkspaceService,
    formal_image_details_by_script_shot,
    formal_image_search_slot,
)
from .formal_image_candidates import (
    _package_search_inputs,
    _present_distinct_candidates,
    allocate_diverse_defaults,
    build_match_explanation,
    load_package_pixel_evidence,
    package_image_similarity,
    save_package_pixel_evidence,
    resolve_legacy_visual_identity,
    search_package_candidates,
    search_package_library_candidates,
)
from .image_understanding import normalize_visual_analysis
from .generation_stages import read_script_draft, read_script_plan, read_visual_binding
from .package_registry import LocalPackageRegistry, PackageRegistryError, package_record_sha256
from .project_image_authority import ProjectImagePoolService
from .project_service import ProjectService
from .run_contract import sha256_file, sha256_json
from .script_type_adapter import metadata_for_category_id, category_id_for_script_type
from .task_service import TaskService
from .version_delivery import VersionDeliveryService, resolve_version_shot_ids
from .version_review_repository import require_no_unsaved_script_draft
from .workspace_store import WorkspaceServiceError


DEFAULT_LIBRARY_CANDIDATE_LIMIT = 12


class FormalDefaultImageService:
    def __init__(
        self,
        *,
        projects: ProjectService,
        tasks: TaskService,
        delivery: VersionDeliveryService,
        artifacts,
        image_pool: ProjectImagePoolService,
        image_workspace: FormalImageWorkspaceService,
        asset_package_resolver: Callable[[str], str | Path | Mapping | None],
        writes_enabled: bool,
        package_registry: LocalPackageRegistry | None = None,
        semantic_policy=None,
        creative_generation_service=None,
    ) -> None:
        self.projects = projects
        self.tasks = tasks
        self.delivery = delivery
        self.artifacts = artifacts
        self.image_pool = image_pool
        self.image_workspace = image_workspace
        self.asset_package_resolver = asset_package_resolver
        self.writes_enabled = writes_enabled
        self.package_registry = package_registry
        self.semantic_policy = semantic_policy
        self.creative_generation_service = creative_generation_service
        self.semantic_cache_root = projects.store.database_path.parent / 'image-semantic-matching' if projects is not None else None
        self._package_media_cache: dict[
            str, tuple[tuple[tuple, ...], dict, list[dict]]
        ] = {}
        self._stage_context_cache: dict[tuple, tuple] = {}
        self._library_query_cache: dict[tuple, dict] = {}
        self._library_inventory_cache: dict[str, dict] = {}
        self._combined_media_cache: dict[tuple, tuple[dict, list[dict]]] = {}
        self._prepared_media_cache: dict[tuple, tuple] = {}
        self._cache_lock = RLock()
        self._package_media_load_locks = tuple(RLock() for _ in range(8))

    def candidates(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        expected_project_version: int,
        view: str = "recommended", query: str = "", page: int = 1,
        page_size: int = 24, shot_no: str = "",
        match_scope: str = "", source: str = "", scene: str = "", subject: str = "",
        brand: str = "", framing: str = "", analysis_status: str = "",
        candidate_limit: int | None = 8,
        block_id: str = "",
        proposal_task_id: str | None = None,
        _requested_shot_nos: tuple[str, ...] | None = None,
    ) -> dict:
        if view not in {"recommended", "library", "browse"}:
            raise WorkspaceServiceError("INVALID_INPUT", "图片浏览方式无效")
        if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 60:
            raise WorkspaceServiceError("INVALID_INPUT", "图片页码或每页数量无效")
        query = str(query or "").strip()
        if len(query) > 300:
            raise WorkspaceServiceError("INVALID_INPUT", "图片查找描述不能超过 300 字")
        if match_scope not in {"", "all", "current_shot"}:
            raise WorkspaceServiceError("INVALID_INPUT", "图片匹配范围无效")
        filters = {key: str(value or "").strip() for key, value in {
            "source": source, "scene": scene, "subject": subject, "brand": brand,
            "framing": framing, "analysis_status": analysis_status,
        }.items()}
        if any(len(value) > 300 for value in filters.values()):
            raise WorkspaceServiceError("INVALID_INPUT", "图片筛选条件过长")
        browse_shot_no = str(shot_no or "").strip()
        if match_scope == "current_shot" and not browse_shot_no and not block_id:
            raise WorkspaceServiceError("INVALID_INPUT", "请先选择当前镜头")
        project, context, plan, draft, binding = self._context(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        creative = getattr(plan, "creative_draft", None)
        if block_id:
            selected_no = next((number for number, identity in (creative or {}).get("block_ids", {}).items()
                                if identity == block_id), None)
            if selected_no is None:
                raise WorkspaceServiceError("IMAGE_SCOPE_MISMATCH", "当前初稿没有该段落，请重新选择目标")
            if browse_shot_no and browse_shot_no != selected_no:
                raise WorkspaceServiceError("IMAGE_SCOPE_MISMATCH", "选图目标与当前段落不一致")
            browse_shot_no = selected_no
        script_revision, working_shots = 0, {}
        if context is not None and self.image_workspace is not None:
            script_revision, working_shots, plan, draft, binding = self.image_workspace.working_image_context(context, plan, draft, binding)
        requested_shot_nos = None if _requested_shot_nos is None else set(_requested_shot_nos)
        if requested_shot_nos is not None and draft.script_rows:
            available_shot_nos = {str(row.get("镜号") or row.get("shot_no") or "") for row in draft.script_rows}
            if not requested_shot_nos or not requested_shot_nos <= available_shot_nos:
                raise WorkspaceServiceError("IMAGE_DECISION_MISMATCH", "默认图片选择与当前镜头不一致")
        if proposal_task_id:
            from .creative_workbench import text_projection
            from .formal_script_workspace import _EDITABLE_FIELDS
            changes = text_projection(self.tasks, auth_token, project_id=project_id,
                project_version=expected_project_version, revision=creative['revision'] if creative else script_revision,
                task_id=proposal_task_id, stage='creative' if creative else 'formal')
            identities = ((creative or {}).get('block_ids') or
                {str(row['shot_no']): identity for identity, row in working_shots.items()})
            by_id = {identity: next(row for row in draft.script_rows if str(row.get('镜号') or row.get('shot_no')) == str(no))
                     for no, identity in identities.items()}
            for change in changes:
                row = by_id.get(change['block_id'])
                if row is None:
                    raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '文字建议中的目标已变化')
                field = change['field'] if creative else _EDITABLE_FIELDS[change['field']]
                if 'before' in change and row.get(field) != change['before']:
                    raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '文字建议的原内容已变化')
                row[field] = change['after']
        package, media = self._package_media(project, plan.brief, (getattr(plan, "constraints", {}) or {}).get("formal_asset_package"))
        if view != "recommended" and match_scope:
            base = {"state": "ready", "shot_count": len(draft.script_rows), "shots": [],
                    "script_revision": script_revision, "basis_label": "当前已保存工作稿"}
            if context is not None:
                base["base_version_id"] = context.version_id
            if creative:
                base.update(target_kind="creative_block", creative_revision=creative["revision"],
                            block_id=block_id, creative_targets=creative["block_ids"])
            return self._library_page(
                project=project, package=package, media=media, plan=plan, draft=draft,
                query=query, page=page, page_size=page_size,
                shot_no=browse_shot_no if match_scope == "current_shot" else "",
                base=base, filters=filters, match_scope=match_scope,
            )
        recommendations = {
            str(item.get("shot_no") or item.get("slot_index") or ""): resolve_legacy_visual_identity(item, media)
            for item in binding.bindings
            if not (view == "recommended" and browse_shot_no)
            or str(item.get("shot_no") or item.get("slot_index") or "") == browse_shot_no
        }
        used: Counter[str] = Counter()
        ranked_by_shot: dict[str, dict] = {}
        shots = []
        if not draft.script_rows:
            if view != "recommended":
                return self._library_page(
                    project=project, package=package, media=media, plan=plan, draft=draft,
                    query=query, page=page, page_size=page_size, shot_no=browse_shot_no,
                    base={"state": "ready", "shot_count": 0, "shots": []},
                    filters=filters,
                )
            return self._library_page(
                project=project, package=package, media=media, plan=plan, draft=draft,
                query=query, page=page, page_size=min(page_size, DEFAULT_LIBRARY_CANDIDATE_LIMIT),
                shot_no="", base={"state": "ready", "shot_count": 0, "shots": []},
                filters=filters, category_recommendations=True,
            )
        details_by_shot = formal_image_details_by_script_shot(
            draft.script_rows, getattr(draft, "detail_rows", ()),
        )
        media_key = (package["verified_manifest_sha256"], sha256_json(media))
        with self._cache_lock:
            prepared = self._prepared_media_cache.get(media_key)
        if prepared is None:
            prepared = _package_search_inputs(media)
            with self._cache_lock:
                if len(self._prepared_media_cache) >= 8:
                    self._prepared_media_cache.pop(next(iter(self._prepared_media_cache)))
                self._prepared_media_cache[media_key] = prepared
        for index, row in enumerate(draft.script_rows, start=1):
            shot_no = str(row.get("镜号") or row.get("shot_no") or "")
            if requested_shot_nos is not None and shot_no not in requested_shot_nos:
                continue
            if view == "recommended" and browse_shot_no and shot_no != browse_shot_no:
                continue
            identity = recommendations.get(shot_no, {})
            planned = (
                plan.shot_plans[index - 1]
                if index <= len(getattr(plan, "shot_plans", ())) else {}
            )
            slot = formal_image_search_slot(row=row, detail=details_by_shot.get(shot_no, {}),
                planned=planned, fallback_index=index)
            preferred = str(identity.get("media_id") or "")
            ranking_key = ("recommended", project_id, project["project_version"],
                project.get("current_version_id"), media_key,
                sha256_json({"brief": plan.brief, "slot": slot, "preferred": preferred}))
            with self._cache_lock:
                result = deepcopy(self._library_query_cache.get(ranking_key))
            if result is None:
                result = search_package_candidates(slot=slot, brief=plan.brief, media=media,
                    preferred_identity=preferred, _prepared=prepared, top_k=None,
                    semantic_policy=self.semantic_policy, semantic_cache_root=self.semantic_cache_root)
                with self._cache_lock:
                    if len(self._library_query_cache) >= 32:
                        self._library_query_cache.pop(next(iter(self._library_query_cache)))
                    self._library_query_cache[ranking_key] = deepcopy(result)
            safe_before_filters = bool(result.get("candidates"))
            filtered = _filter_candidates(result.get("candidates") or [], media, query, filters)
            result["facets"] = _candidate_facets(result.get("candidates") or [], media)
            ranked = _limit_candidate_groups(filtered, candidate_limit)
            result["filtered_groups"] = len({item["duplicate_group_id"] for item in filtered})
            result["candidates"] = ranked
            ranked_by_shot[shot_no] = result
            if not ranked:
                shots.append({
                    "state": "gap",
                    "shot_no": shot_no,
                    "visual": str(row.get("画面描述") or ""),
                    "selected_candidate_key": "",
                    "candidates": [],
                    "identity_mapping": identity,
                    "rejected_count": len(result.get("rejected") or []),
                    "gap_reason": ("当前筛选没有匹配图片，请调整或清除筛选"
                                   if safe_before_filters else _aggregated_gap_reason(result)),
                    "supplement_upload_available": True,
                    "temporary_text_basis_available": True,
                    "warnings": (["当前筛选没有匹配图片，请调整或清除筛选"] if safe_before_filters else
                                 ["当前镜头没有通过权威安全规则的类别图片；请补图或暂准文字依据。"]),
                    'semantic_assessment': result.get('semantic_assessment'),
                })
                continue
            shots.append({
                "state": "ready",
                "shot_no": shot_no,
                "visual": str(row.get("画面描述") or ""),
                "selected_candidate_key": "",
                "candidates": [
                    self._public_candidate(item, project_id, media, ranked)
                    for item in ranked
                ],
                "identity_mapping": identity,
                "rejected_count": len(result.get("rejected") or []),
                'semantic_assessment': result.get('semantic_assessment'),
                'warnings': [result['semantic_assessment']['message']] if result.get('semantic_assessment') else [],
            })
        allocation = allocate_diverse_defaults(ranked_by_shot)
        if browse_shot_no and view == "recommended" and not shots:
            raise WorkspaceServiceError("NOT_FOUND", "当前脚本中没有这个镜头")
        for shot in shots:
            shot['shot_id'] = next((identity for identity, value in working_shots.items()
                                   if str(value['shot_no']) == shot['shot_no']), '')
            selected = allocation["selected_by_shot"].get(shot["shot_no"], "")
            shot["selected_candidate_key"] = selected
            if selected:
                used[selected] += 1
        for shot in shots:
            count = used[shot["selected_candidate_key"]]
            shot["reuse_count"] = count
            warnings = list(shot.get("warnings") or [])
            if count > 1:
                warnings.append(f"该图将用于 {count} 镜，保存前需逐镜确认")
                selected_candidate = next((
                    candidate for candidate in shot.get("candidates") or []
                    if candidate.get("candidate_key") == shot["selected_candidate_key"]
                ), None)
                if selected_candidate is not None:
                    selected_candidate["match_explanation"]["reuse"] = (
                        f"当前默认分配会在 {count} 镜复用该图，保存前需逐镜确认"
                    )
            shot["warnings"] = warnings
        ready_count = sum(item["state"] == "ready" for item in shots)
        gap_count = len(shots) - ready_count
        warnings = []
        if len(media) < len(shots):
            warnings.append("候选数少于镜头数，部分图片需要复用并逐镜确认")
        if gap_count and not (query or any(filters.values())):
            warnings.append(
                f"{gap_count} 镜没有安全类别候选；可先采用其余 {ready_count} 镜，缺口仍会阻断审核和交付"
            )
        response = {
            "script_revision": script_revision, "basis_label": "当前已保存工作稿",
            "state": ("ready" if query or any(filters.values()) else
                      "ready" if not gap_count else "partial" if ready_count else "blocked"),
            "project_version": project["project_version"],
            "package": {
                "package_id": package["package_id"],
                "package_version": int(package.get("package_version") or 1),
                "category_id": package["category_id"],
                "manifest_sha256": package["verified_manifest_sha256"],
                "source_scope_sha256": package.get("source_scope_sha256"),
            },
            "shot_count": len(shots), "candidate_count": len(media),
            "ready_shot_count": ready_count, "gap_shot_count": gap_count,
            "shots": shots,
            "warnings": warnings,
            "query": query, "filters": filters,
            "match_scope": match_scope or ("current_shot" if browse_shot_no else ""),
            "facets": next(iter(ranked_by_shot.values()), {}).get("facets", {}),
            "filtered_groups": next(iter(ranked_by_shot.values()), {}).get("filtered_groups", 0),
        }
        if context is not None:
            identities = ({str(shot["shot_no"]): identity for identity, shot in working_shots.items()}
                          if working_shots else resolve_version_shot_ids(self.artifacts, context, draft.script_rows))
            response["base_version_id"] = context.version_id
            for shot in shots:
                shot["shot_id"] = identities[str(shot["shot_no"])]
        if creative:
            response.update(target_kind="creative_block", creative_revision=creative["revision"],
                            block_id=block_id, creative_targets=creative["block_ids"])
            for shot in shots:
                shot["block_id"] = creative["block_ids"][str(shot["shot_no"])]
        if view != "recommended":
            return self._library_page(
                project=project, package=package, media=media, plan=plan, draft=draft,
                query=query, page=page, page_size=page_size, shot_no=browse_shot_no,
                base=response, prepared=prepared, filters=filters,
            )
        return response

    def _library_matches(self, *, plan, draft, media: list[dict], shot_no: str = "", prepared=None) -> dict:
        if not shot_no:
            return search_package_library_candidates(
                brief=plan.brief, media=media, top_k=None, browse=True,
            )
        details = formal_image_details_by_script_shot(
            draft.script_rows, getattr(draft, "detail_rows", ()),
        )
        for index, row in enumerate(draft.script_rows, start=1):
            if str(row.get("镜号") or row.get("shot_no") or "") != shot_no:
                continue
            plans = getattr(plan, "shot_plans", ())
            return search_package_candidates(
                slot=formal_image_search_slot(
                    row=row, detail=details.get(shot_no, {}),
                    planned=plans[index - 1] if index <= len(plans) else {}, fallback_index=index,
                ),
                brief=plan.brief, media=media, top_k=None, _prepared=prepared,
                semantic_policy=self.semantic_policy, semantic_cache_root=self.semantic_cache_root,
            )
        raise WorkspaceServiceError("NOT_FOUND", "当前脚本中没有这个镜头")

    def _library_page(
        self, *, project: dict, package: dict, media: list[dict], plan, draft,
        query: str, page: int, page_size: int, shot_no: str, base: dict, prepared=None,
        filters: dict | None = None, match_scope: str = "", category_recommendations: bool = False,
    ) -> dict:
        # Package/index/blob integrity is checked before reaching this cache. Permissions
        # and the current project/version are resolved on every request by _context.
        media_sha256 = sha256_json(media)
        query_key = (project["project_id"], project["project_version"], project.get("current_version_id"),
            package["verified_manifest_sha256"], media_sha256, category_recommendations, sha256_json({
            "brief": plan.brief, "rows": draft.script_rows,
            "details": getattr(draft, "detail_rows", ()),
            "plans": getattr(plan, "shot_plans", ()), "shot_no": shot_no,
        }))
        with self._cache_lock:
            result = deepcopy(self._library_query_cache.get(query_key))
        if result is None:
            result = (search_package_library_candidates(brief=plan.brief, media=media, top_k=None)
                      if category_recommendations else
                      self._library_matches(plan=plan, draft=draft, media=media, shot_no=shot_no, prepared=prepared))
            with self._cache_lock:
                if len(self._library_query_cache) >= 32:
                    self._library_query_cache.pop(next(iter(self._library_query_cache)))
                self._library_query_cache[query_key] = deepcopy(result)
        media_by_id = {item["media_id"]: item for item in media}
        filters = filters or {}
        facets = _candidate_facets(result.get("candidates") or [], media)
        groups: dict[str, list[dict]] = {}
        for candidate in _filter_candidates(result.get("candidates") or [], media, query, filters):
            group = str(candidate.get("duplicate_group_id") or candidate["image_id"])
            groups.setdefault(group, []).append(dict(candidate))
        ordered_groups = list(groups.values())
        with self._cache_lock:
            inventory = self._library_inventory_cache.get(media_sha256)
        if inventory is None:
            distinct = _present_distinct_candidates(
                [{"image_id": item["media_id"]} for item in media], media_by_id, None,
            )
            inventory = {
                "inventory_groups": len({item["duplicate_group_id"] for item in distinct}),
                "status_counts": {
                    "pending_analysis": sum(item.get("analysis_state") == "pending" for item in media),
                    "failed_analysis": sum(item.get("analysis_state") == "failed" for item in media),
                    "forbidden": sum(bool(set([*(item.get("negative_flags") or []), *(item.get("quality_flags") or [])]).intersection({"forbidden_content", "sensitive_content"})) for item in media),
                    "without_script_row": sum(not item.get("source_links") for item in media),
                    "identity_failed": 0,
                },
            }
            with self._cache_lock:
                if len(self._library_inventory_cache) >= 8:
                    self._library_inventory_cache.pop(next(iter(self._library_inventory_cache)))
                self._library_inventory_cache[media_sha256] = inventory
        start, end = (page - 1) * page_size, page * page_size
        selected = []
        for values in ordered_groups[start:end]:
            # A search can find a secondary source but not its original representative.
            # Keep that matching identity visible and keep every page's static group whole.
            for index, item in enumerate(values):
                item["duplicate_of"] = values[0]["image_id"] if index else ""
                item["duplicate_count"] = len(values) - 1
                selected.append(item)
        return {
            **base, "view": "recommended" if category_recommendations else "library",
            "selection_mode": "category_library_browse" if draft.script_rows else "category_library_multi",
            "project_version": project["project_version"],
            "package": {"package_id": package["package_id"], "package_version": int(package.get("package_version") or 1),
                        "category_id": package["category_id"], "manifest_sha256": package["verified_manifest_sha256"],
                        "source_scope_sha256": package.get("source_scope_sha256")},
            "candidate_count": len(media), "safe_candidate_count": len(result.get("candidates") or []),
            "library_candidates": [self._public_candidate(item, project["project_id"], media, selected) for item in selected],
            'semantic_assessment': result.get('semantic_assessment'),
            "page": page, "page_size": page_size, "query": query, "shot_no": shot_no,
            "match_scope": match_scope or ("current_shot" if shot_no else "all"),
            "facets": facets, "filters": filters,
            "total_groups": len(groups), "total_images": sum(map(len, ordered_groups)),
            "inventory_records": len(media),
            "inventory_groups": inventory["inventory_groups"],
            "filtered_groups": len(groups),
            "displayed_groups": len(ordered_groups[start:end]),
            "displayed_images": len(selected),
            "page_records": len(selected),
            "cumulative_groups": min(end, len(groups)),
            "status_counts": dict(inventory["status_counts"]),
            "status_counts_overlap": True,
            "page_groups": len(ordered_groups[start:end]), "has_more": end < len(groups),
            "warnings": ["可多选类别图片；脚本生成后仍需逐镜搜索并确认匹配关系" if category_recommendations
                         else "选择图片后仍需核对当前镜头；图库参考不代表本项目实拍。"],
        }

    def preview(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        expected_project_version: int, media_id: str,
        variant: str = "original", version_id: str = "",
    ) -> tuple[bytes, str]:
        if variant not in {"original", "thumbnail"}:
            raise WorkspaceServiceError("INVALID_INPUT", "图片预览规格无效")
        project, _context, plan, _draft, _binding = self._context(
            auth_token, workspace_id, project_id, expected_project_version,
            version_id=version_id,
        )
        _package, media = self._package_media(project, plan.brief, (getattr(plan, "constraints", {}) or {}).get("formal_asset_package"))
        candidate = next((item for item in media if item["media_id"] == media_id), None)
        if candidate is None:
            raise WorkspaceServiceError("NOT_FOUND", "默认图片候选不存在")
        raw = Path(candidate["path"]).read_bytes()
        from hashlib import sha256
        if variant == "original":
            if sha256(raw).hexdigest() != candidate["content_sha256"]:
                raise WorkspaceServiceError("ASSET_PACKAGE_BLOB_DRIFT", "资产包媒体 SHA-256 漂移")
            return raw, _image_mime(raw)
        from .image_preview import image_preview_bytes
        try:
            return image_preview_bytes(raw, content_sha256=candidate["content_sha256"], variant=variant,
                cache_root=self.image_pool.artifacts.root.parent / "derived-image-previews")
        except ValueError as exc:
            if sha256(raw).hexdigest() != candidate["content_sha256"]:
                raise WorkspaceServiceError("ASSET_PACKAGE_BLOB_DRIFT", "资产包媒体 SHA-256 漂移") from exc
            raise WorkspaceServiceError("ASSET_PACKAGE_MEDIA_INVALID", "默认图片格式不受支持") from exc

    def propose(
        self, auth_token: str, *, agent_session_id: str, workspace_id: str,
        project_id: str, expected_project_version: int,
        selections: list[dict], idempotency_key: str,
        allowed_shot_ids: list[str] | tuple[str, ...] | None = None,
        expected_script_revision: int | None = None,
    ) -> dict:
        session = (
            self.tasks.get_agent_session(auth_token, agent_session_id)
            if agent_session_id else
            self.tasks.create_agent_session(auth_token, project_id=project_id)
        )
        agent_session_id = str(session["agent_session_id"])
        if session["project_id"] != project_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "Agent 会话不属于当前项目")
        if expected_script_revision is not None and (type(expected_script_revision) is not int or expected_script_revision < 0):
            raise WorkspaceServiceError("INVALID_INPUT", "当前稿修订号无效")
        request_digest = sha256_json({
            "project_id": project_id, "version": expected_project_version,
            "selections": selections, "idempotency_key": idempotency_key,
            **({"expected_script_revision": expected_script_revision} if expected_script_revision is not None else {}),
            **({"allowed_shot_ids": list(allowed_shot_ids)} if allowed_shot_ids is not None else {}),
        })
        project = self.projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "项目不属于当前工作区")
        # Return the original immutable proposal before freshness checks. Execution
        # still checks project/script locks; a lost proposal receipt creates no twin.
        with self.image_pool.store.connection() as connection:
            previous = connection.execute(
                """SELECT t.task_id, t.payload_sha256 FROM tasks t
                JOIN task_events e ON e.task_id=t.task_id
                WHERE t.project_id=? AND t.actor_id=? AND t.task_type='formal_default_image_adoption'
                AND json_extract(e.details_json, '$.internal_formal_parameters.idempotency_key')=?
                ORDER BY e.created_at LIMIT 1""",
                (project_id, session["actor_id"], str(idempotency_key)),
            ).fetchone()
        if previous:
            if previous["payload_sha256"] != request_digest:
                raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "同一采用请求不能用于不同内容")
            prior_task = self.tasks.get_task(auth_token, previous["task_id"])
            for event in reversed(self.tasks.list_task_events(auth_token, task_id=previous["task_id"])):
                details = event.get("details") or {}
                if details.get("internal_formal_parameters") and details.get("response"):
                    return {"task_id": previous["task_id"], "status": prior_task["status"], "response": details["response"]}
        if project["workspace_id"] != workspace_id or project["project_version"] != expected_project_version:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目版本已变化")
        if project.get("current_version_id") and expected_script_revision is None:
            with self.image_pool.store.connection() as connection:
                require_no_unsaved_script_draft(connection, workspace_id=workspace_id,
                    project_id=project_id, base_version_id=project["current_version_id"])
        # Validate against the full authoritative draft before ranking only the
        # submitted selections. Confirming two images must not query every shot.
        _, adoption_context, adoption_plan, adoption_draft, adoption_binding = self._context(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        adoption_revision, adoption_shots = 0, {}
        if adoption_context is not None and self.image_workspace is not None:
            adoption_revision, adoption_shots, adoption_plan, adoption_draft, adoption_binding = self.image_workspace.working_image_context(
                adoption_context, adoption_plan, adoption_draft, adoption_binding,
            )
        if expected_script_revision is not None and adoption_revision != expected_script_revision:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "当前稿已变化，请重新读取后采用图片")
        shot_nos = [str(row.get("镜号") or row.get("shot_no") or "") for row in adoption_draft.script_rows]
        selected_shots = [str(item.get("shot_no") or "") for item in selections]
        if (not selections or len(selected_shots) != len(set(selected_shots))
                or (shot_nos and not set(selected_shots) <= set(shot_nos))):
            raise WorkspaceServiceError("IMAGE_DECISION_MISMATCH", "默认图片选择与当前候选不一致")
        if allowed_shot_ids is not None:
            identities = ({str(value["shot_no"]): identity for identity, value in adoption_shots.items()}
                          if adoption_shots else resolve_version_shot_ids(self.artifacts, adoption_context, adoption_draft.script_rows)
                          if adoption_context is not None else {})
            allowed = set(allowed_shot_ids)
            if (not allowed or len(allowed) != len(allowed_shot_ids) or "" in allowed
                    or not allowed <= set(identities.values())
                    or any(identities.get(number) not in allowed for number in selected_shots)):
                raise WorkspaceServiceError("IMAGE_SCOPE_MISMATCH", "素材选择超出本次明确镜头范围，请重新选择")
        preview = self.candidates(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=expected_project_version,
            _requested_shot_nos=tuple(selected_shots),
        )
        if expected_script_revision is not None and preview.get("script_revision") != expected_script_revision:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "当前稿已变化，请重新读取后采用图片")
        evidence_by_shot = {
            item["shot_no"]: {candidate["candidate_key"]: candidate for candidate in item["candidates"]}
            for item in preview["shots"]
        }
        library_evidence = {item["candidate_key"]: item for item in preview.get("library_candidates") or []}
        is_library_multi = preview.get("selection_mode") == "category_library_multi"
        if (
            not selections
            or (is_library_multi and len(selected_shots) != len(set(selected_shots)))
            or (not is_library_multi and (len(selected_shots) != len(set(selected_shots)) or not set(selected_shots) <= set(shot_nos)))
        ):
            raise WorkspaceServiceError("IMAGE_DECISION_MISMATCH", "默认图片选择与当前候选不一致")
        missing_from_preview = [
            item for item in selections
            if str(item.get("candidate_key") or "") not in (
                library_evidence if is_library_multi else evidence_by_shot.get(str(item.get("shot_no") or ""), {})
            )
        ]
        if missing_from_preview:
            current_project, _context, plan, draft, _binding = self._context(
                auth_token, workspace_id, project_id, expected_project_version,
            )
            if expected_script_revision is not None:
                if _context is None or self.image_workspace is None:
                    raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "当前稿暂不可用")
                revision, _shots, plan, draft, _binding = self.image_workspace.working_image_context(_context, plan, draft, _binding)
                if revision != expected_script_revision:
                    raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "当前稿已变化，请重新读取后采用图片")
            _package, media = self._package_media(
                current_project, plan.brief, (getattr(plan, "constraints", {}) or {}).get("formal_asset_package"),
            )
            prepared = _package_search_inputs(media) if not is_library_multi else None
            targets = [""] if is_library_multi else list(dict.fromkeys(
                str(item.get("shot_no") or "") for item in missing_from_preview
                if str(item.get("shot_no") or "") in evidence_by_shot
            ))
            for target in targets:
                result = self._library_matches(plan=plan, draft=draft, media=media, shot_no=target, prepared=prepared)
                candidates = {
                    item["image_id"]: self._public_candidate(item, project_id, media, result["candidates"])
                    for item in result.get("candidates") or []
                }
                if is_library_multi:
                    library_evidence.update(candidates)
                else:
                    evidence_by_shot[target].update(candidates)
        allowed = {
            shot: set(candidates) for shot, candidates in evidence_by_shot.items()
        }
        if is_library_multi:
            library_allowed = set(library_evidence)
            valid = bool(selections) and len(selected_shots) == len(set(selected_shots)) and all(
                str(item.get("candidate_key") or "") in library_allowed for item in selections
            )
        else:
            valid = bool(shot_nos) and set(selected_shots) <= set(shot_nos) and all(
                str(item.get("candidate_key") or "") in allowed.get(item["shot_no"], set())
                for item in selections
            )
        if not valid:
            raise WorkspaceServiceError("IMAGE_DECISION_MISMATCH", "默认图片选择与当前候选不一致")
        frozen_selections = []
        for item in selections:
            shot, key = str(item["shot_no"]), str(item["candidate_key"])
            candidate = (library_evidence if is_library_multi else evidence_by_shot[shot])[key]
            frozen = {"shot_no": shot, "media_id": key}
            if candidate.get("content_sha256"):
                frozen.update({"content_sha256": candidate["content_sha256"], "sources": candidate.get("sources") or []})
            frozen_selections.append(frozen)
        task = self.tasks.create_task(
            auth_token, agent_session_id=agent_session_id,
            task_type="formal_default_image_adoption",
            payload_sha256=request_digest,
            expected_project_version=expected_project_version,
        )
        task = self.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="understanding", details={"formal_context_version": expected_project_version},
        )
        if not self.writes_enabled:
            response = {
                "type": "read_only_diagnostic", "title": "只读诊断",
                "summary": "候选已校验，但默认图片采用写能力当前关闭。",
                "error_code": "FORMAL_DEFAULT_IMAGE_ADOPTION_DISABLED",
                "next_steps": ["启用 Formal 写能力后重新提交。"],
            }
            task = self.tasks.transition_task(
                auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
                status="blocked", details={"response": response},
            )
            return {"task_id": task["task_id"], "status": task["status"], "response": response}
        pending_count = 0 if is_library_multi else len(shot_nos) - len(selected_shots)
        package = preview["package"]
        parameters = {
            "project_id": project_id, "expected_project_version": expected_project_version,
            "package_id": package["package_id"], "package_version": package["package_version"],
            "package_manifest_sha256": package["manifest_sha256"],
            "source_scope_sha256": package.get("source_scope_sha256"),
            "selections": frozen_selections,
            "pending_shot_count": pending_count,
            "idempotency_key": str(idempotency_key),
        }
        if expected_script_revision is not None:
            parameters["expected_script_revision"] = expected_script_revision
        if allowed_shot_ids is not None:
            parameters["allowed_shot_ids"] = list(allowed_shot_ids)
        digest = sha256_json({"action": "adopt_formal_default_images", "parameters": parameters})
        counts = Counter(item["media_id"] for item in parameters["selections"])
        response = {
            "type": "action_proposal", "title": "确认采用参考图",
            "summary": f"为 {len(selections)} 镜采用 {len(counts)} 张参考图",
            "action": "adopt_formal_default_images", "proposal_digest": digest,
            "risk": "R2", "parameters": {
                "shot_count": len(selections), "unique_image_count": len(counts),
                "reused_image_count": sum(1 for count in counts.values() if count > 1),
                "pending_shot_count": pending_count,
            },
            "will_change": "加入项目素材并保留来源；随后逐镜核对。",
            "will_not_change": "不会伪造 Brief 图片、修改历史版本、发布状态或 CURRENT",
            "calls_models": False, "model_call_statement": "采用过程不调用模型",
            "impact_scope": "当前项目图片池",
            "warnings": [
                *([f"其中 {sum(count > 1 for count in counts.values())} 张图片需要复用，请逐镜核对。"]
                  if any(count > 1 for count in counts.values()) else []),
                *(
                    [f"还有 {pending_count} 镜未选图，可稍后补充。"]
                    if pending_count else []
                ),
            ],
            "next_steps": ["返回逐镜配图，核对后保存新版本。"],
        }
        task = self.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="proposed", details={"response": response, "internal_formal_parameters": parameters},
        )
        return {"task_id": task["task_id"], "status": task["status"], "response": response}

    @staticmethod
    def _adoption_parameters_digest(parameters: dict) -> str:
        # Ignore only transport scope duplication and absent optional fields.
        keys = ("project_id", "expected_project_version", "expected_script_revision", "package_id",
                "package_version", "package_manifest_sha256", "source_scope_sha256", "selections",
                "pending_shot_count", "idempotency_key", "allowed_shot_ids")
        return sha256_json({key: parameters[key] for key in keys if parameters.get(key) is not None})

    def read_adoption_receipt(self, auth_token: str, *, task_id: str, parameters: dict) -> dict | None:
        task = self.tasks.get_task(auth_token, task_id)
        if task["project_id"] != parameters["project_id"] or task["task_type"] != "formal_default_image_adoption":
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "采用凭证不属于当前任务")
        digest = self._adoption_parameters_digest(parameters)
        events = self.tasks.list_task_events(auth_token, task_id=task_id)
        frozen = next(((event.get("details") or {}).get("internal_formal_parameters") for event in events
                       if (event.get("details") or {}).get("internal_formal_parameters")), None)
        if frozen is None or self._adoption_parameters_digest(frozen) != digest:
            raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "采用参数与原任务提案不一致")
        for event in reversed(events):
            receipt = (event.get("details") or {}).get("formal_default_image_adoption_receipt")
            if not receipt:
                continue
            if (receipt.get("task_id") != task_id or receipt.get("actor_id") != task["actor_id"]
                    or receipt.get("project_id") != task["project_id"] or receipt.get("parameters_sha256") != digest):
                raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "采用凭证与本次操作不一致")
            response = receipt.get("response") or {}
            if response.get("status") == "completed" and len(response.get("adopted_selections") or []) == len(parameters["selections"]):
                return deepcopy(response)
        return None

    def execute(self, auth_token: str, *, project: dict, parameters: dict, task_id: str | None = None) -> dict:
        if task_id:
            receipt = self.read_adoption_receipt(auth_token, task_id=task_id, parameters=parameters)
            if receipt is not None:
                return receipt
        _project, context, plan, _draft, _binding = self._context(
            auth_token, project["workspace_id"], project["project_id"],
            parameters["expected_project_version"],
        )
        working_shots = {}
        if parameters.get("expected_script_revision") is not None:
            if context is None or self.image_workspace is None:
                raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "当前稿暂不可用")
            revision, working_shots, plan, _draft, _binding = self.image_workspace.working_image_context(context, plan, _draft, _binding)
            if revision != parameters["expected_script_revision"]:
                raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "当前稿已变化，请重新读取后采用图片")
        elif context is not None:
            with self.image_pool.store.connection() as connection:
                require_no_unsaved_script_draft(connection, workspace_id=project["workspace_id"],
                    project_id=project["project_id"], base_version_id=context.version_id)
        if parameters.get("allowed_shot_ids") is not None:
            identities = ({str(shot["shot_no"]): identity for identity, shot in working_shots.items()}
                          if working_shots else resolve_version_shot_ids(self.artifacts, context, _draft.script_rows) if context else {})
            allowed = set(parameters["allowed_shot_ids"])
            if (not allowed or len(allowed) != len(parameters["allowed_shot_ids"])
                    or not allowed <= set(identities.values())
                    or any(identities.get(str(item["shot_no"])) not in allowed for item in parameters["selections"])):
                raise WorkspaceServiceError("IMAGE_SCOPE_MISMATCH", "素材选择超出本次明确镜头范围，请重新选择")
        package, media = self._package_media(project, plan.brief, (getattr(plan, "constraints", {}) or {}).get("formal_asset_package"))
        if (
            package["package_id"] != parameters["package_id"]
            or int(package.get("package_version") or 1) != parameters["package_version"]
            or package["verified_manifest_sha256"] != parameters["package_manifest_sha256"]
            or (parameters.get("source_scope_sha256") and package["source_scope_sha256"] != parameters["source_scope_sha256"])
        ):
            raise WorkspaceServiceError("ASSET_PACKAGE_DRIFT", "资产包版本在提案后已变化")
        by_id = {item["media_id"]: item for item in media}
        selected = []
        for item in parameters["selections"]:
            candidate = by_id.get(item["media_id"])
            if candidate is None:
                raise WorkspaceServiceError("ASSET_PACKAGE_DRIFT", "默认图片候选在提案后已变化")
            if item.get("content_sha256") and (
                item["content_sha256"] != candidate["content_sha256"]
                or item.get("sources") != _public_sources(candidate.get("source_links") or [], candidate.get("source") or {})
            ):
                raise WorkspaceServiceError("ASSET_PACKAGE_DRIFT", "默认图片内容或来源在提案后已变化")
            # Legacy catalogs may expose two media IDs for the same verified
            # pixels while retaining the first record's aggregated origins.
            # Explicitly pass the selected ID and its frozen source to the pool;
            # never infer an image from tuple order, filename or a bare SHA.
            candidate = deepcopy(candidate)
            origins = candidate.get("package_sources") or []
            source = candidate.get("source") or {}
            raw_source = {"file": str(source.get("source_file") or source.get("file") or ""),
                          "sheet": str(source.get("source_sheet") or source.get("sheet") or ""),
                          "row": int(source.get("source_row") or source.get("row") or 1)}
            for origin in origins:
                origin_candidate = by_id.get(origin.get("media_id"))
                if origin_candidate and origin_candidate["content_sha256"] == candidate["content_sha256"] and not origin_candidate.get("source_links"):
                    origin_source = origin_candidate.get("source") or {}
                    if origin.get("sources") == [_public_source(origin_source)]:
                        origin["sources"] = [{
                            "file": str(origin_source.get("source_file") or origin_source.get("file") or ""),
                            "sheet": str(origin_source.get("source_sheet") or origin_source.get("sheet") or ""),
                            "row": int(origin_source.get("source_row") or origin_source.get("row") or 1),
                        }]
            if origins and not any(origin.get("media_id") == candidate["media_id"] for origin in origins):
                candidate["package_sources"] = [*origins, {
                    "package_id": parameters["package_id"],
                    "package_version": parameters["package_version"],
                    "package_manifest_sha256": parameters["package_manifest_sha256"],
                    "media_id": candidate["media_id"],
                    "sources": (_public_sources(candidate["source_links"], {}) if candidate.get("source_links") else [raw_source]),
                }]
            selected.append(candidate)
        if context is None:
            brief_version_id = str(plan.confirmed_brief_input["brief_version_id"])
            snapshot_id = str(plan.confirmed_brief_input["brief_snapshot_id"])
        else:
            brief_version_id, snapshot_id = self.image_workspace._brief_binding(
                context, plan.confirmed_brief_input,
            )
        adopted = self.image_pool.adopt_package_images(
            auth_token, workspace_id=project["workspace_id"], project_id=project["project_id"],
            brief_version_id=brief_version_id, brief_snapshot_id=snapshot_id,
            expected_project_lock_version=(
                context.project_lock_version if context is not None else project["project_version"]
            ),
            package_id=parameters["package_id"], package_version=parameters["package_version"],
            candidates=tuple({item["media_id"]: item for item in selected}.values()),
        )
        # Resolve the persisted media/content mapping, never tuple order: two
        # library identities may intentionally point to one project image.
        with self.image_pool.store.connection() as connection:
            links = connection.execute(
                """SELECT l.media_id, l.content_sha256, l.project_image_id
                FROM project_image_package_source_links l
                JOIN project_images i ON i.project_image_id=l.project_image_id
                WHERE i.project_id=?""",
                (project["project_id"],),
            ).fetchall()
        by_identity = {(row["media_id"], row["content_sha256"]): row["project_image_id"] for row in links}
        identities = ({str(shot["shot_no"]): identity for identity, shot in working_shots.items()}
                          if working_shots else resolve_version_shot_ids(self.artifacts, context, _draft.script_rows) if context else {})
        adopted_selections = [{
            "shot_id": identities.get(str(item["shot_no"]), ""),
            "shot_no": str(item["shot_no"]),
            "image_key": by_identity[(item["media_id"], by_id[item["media_id"]]["content_sha256"])],
            "content_sha256": by_id[item["media_id"]]["content_sha256"],
        } for item in parameters["selections"]]
        response = {
            "type": "formal_default_images_adopted", "status": "completed",
            "base_version_id": context.version_id if context is not None else None,
            "project_version": project["project_version"],
            "adopted_selections": adopted_selections,
            "adopted_image_count": len(adopted),
            "shot_count": len(parameters["selections"]),
            "pending_shot_count": int(parameters.get("pending_shot_count") or 0),
            "review_eligible": False,
            "delivery_eligible": False,
            "summary": "类别默认图片已进入项目图片池，请继续逐镜确认。",
        }
        if task_id:
            task = self.tasks.get_task(auth_token, task_id)
            self.tasks.record_task_progress(auth_token, task_id=task_id, expected_task_version=task["task_version"],
                details={"formal_default_image_adoption_receipt": {
                    "task_id": task_id, "actor_id": task["actor_id"], "project_id": project["project_id"],
                    "parameters_sha256": self._adoption_parameters_digest(parameters), "response": response,
                }})
        return response

    def progress_reference(self, auth_token: str, *, workspace_id: str, project_id: str,
                           expected_project_version: int, candidate_key: str) -> dict:
        references = self.progress_references(auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=expected_project_version, candidate_keys=[candidate_key])
        if candidate_key not in references:
            raise WorkspaceServiceError("IMAGE_CANDIDATE_INVALID", "暂选图片已不在当前创意库中")
        return references[candidate_key]

    def progress_references(self, auth_token: str, *, workspace_id: str, project_id: str,
                            expected_project_version: int, candidate_keys: list[str],
                            similarity_items: list[dict] | None = None) -> dict[str, dict]:
        """Resolve a selection set from one verified catalog; absent IDs stay absent."""
        project, _context, plan, _draft, _binding = self._context(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        _package, media = self._package_media(
            project, plan.brief, (getattr(plan, "constraints", {}) or {}).get("formal_asset_package"),
        )
        by_id = {item["media_id"]: item for item in media}
        references = {}
        for candidate_key in dict.fromkeys(candidate_keys):
            item = by_id.get(candidate_key)
            if item is None:
                continue
            if similarity_items is not None:
                similarity_items.append({"image_id": "library:" + candidate_key, "path": item["path"]})
            sources = _public_sources(item.get("source_links") or [], item.get("source") or {})
            from .image_preview import formal_image_preview_url
            origin = item["package_sources"][0]
            references[candidate_key] = {
                "kind": "library", "candidate_key": candidate_key,
                "package_id": origin["package_id"], "package_version": origin["package_version"],
                "package_manifest_sha256": origin["package_manifest_sha256"],
                "package_sources": item["package_sources"],
                "media_id": item["media_id"], "content_sha256": item["content_sha256"], "sources": sources,
                "candidate": {
                    "candidate_key": candidate_key, "media_id": item["media_id"],
                    "content_sha256": item["content_sha256"], "caption": item["caption"],
                    "actual_caption": item["caption"], "template_usage": item.get("source_semantic_text", ""),
                    "sources": sources, "source_category": _source_category(sources),
                    "preview_url": formal_image_preview_url(item, project_id=project_id,
                        workspace_id=workspace_id, expected_project_version=expected_project_version),
                },
            }

        return references

    def _context(self, auth_token, workspace_id, project_id, expected_project_version, *, version_id=""):
        project = self.projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
        if project["project_version"] != expected_project_version:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目版本已变化")
        requested_version = version_id
        version_id = str(version_id or project.get("current_version_id") or "")
        if not version_id:
            with self.image_pool.store.connection() as connection:
                row = connection.execute(
                    """SELECT c.confirmation_id, c.brief_version_id, c.brief_snapshot_id,
                              c.confirmation_status, c.unresolved_conflict_count,
                              v.payload_ref, v.payload_sha256
                       FROM projects p
                       JOIN brief_confirmations c
                         ON c.project_id=p.project_id AND c.brief_snapshot_id=p.current_brief_snapshot_id
                       JOIN brief_versions v ON v.brief_version_id=c.brief_version_id
                       WHERE p.project_id=? AND p.workspace_id=?
                         AND c.confirmation_status='confirmed'
                         AND c.unresolved_conflict_count=0""",
                    (project_id, workspace_id),
                ).fetchone()
            if row is None:
                raise WorkspaceServiceError("CONFIRMED_BRIEF_REQUIRED", "请先确认 Brief")
            script_type = metadata_for_category_id(project["category_id"]).display_name
            evidence = {
                "workspace_id": workspace_id, "project_id": project_id,
                "confirmation_id": str(row["confirmation_id"]),
                "brief_version_id": str(row["brief_version_id"]),
                "brief_snapshot_id": str(row["brief_snapshot_id"]),
                "confirmation_status": str(row["confirmation_status"]),
                "unresolved_conflict_count": int(row["unresolved_conflict_count"]),
                "payload": {"ref": str(row["payload_ref"]), "sha256": str(row["payload_sha256"])},
            }
            initial_rows, creative = [], None
            if self.creative_generation_service is not None:
                from .creative_generation import repository
                from .creative_revision import effective_brief_values
                state = repository(self.creative_generation_service, auth_token, workspace_id, project_id).read()
                current = self.creative_generation_service.services.briefs.read_current_formal(
                    auth_token, workspace_id=workspace_id, project_id=project_id)
                brief_values = effective_brief_values(current)
                rows = (state["body"].get("draft") or {}).get("shots") or []
                initial_rows = [{**deepcopy(item["fields"]), "镜号": str(item["shot_no"])} for item in rows]
                if rows:
                    creative = {"revision": state["revision"], "draft_id": state["body"]["draft"]["draft_id"],
                                "block_ids": {str(item["shot_no"]): item["shot_id"] for item in rows}}
            else:
                brief_values = {}
            return (
                project, None,
                SimpleNamespace(
                    brief={**brief_values, "脚本类型": script_type, "项目名称": project.get("name") or "当前项目"},
                    confirmed_brief_input=evidence,
                    creative_draft=creative,
                ),
                SimpleNamespace(script_rows=initial_rows), SimpleNamespace(bindings=[]),
            )
        context = self.delivery.resolve(
            auth_token, project_id, version_id,
            expected_project_version=expected_project_version,
            ancestor_of=str(project.get("current_version_id") or "") if requested_version else None,
            reuse_immutable=True,
        )
        cache_key = (context.run_id, context.manifest.sha256,
                     tuple((key, value["sha256"]) for key, value in context.artifacts.as_dict().items()))
        with self._cache_lock:
            cached = self._stage_context_cache.get(cache_key)
        if cached and _path_fingerprint(cached[0]) == cached[1]:
            plan, draft, binding = deepcopy(cached[2])
            return project, context, plan, draft, binding
        paths = tuple(self.artifacts.resolve_stage_artifact(context.run_id, stage,
            expected_manifest_sha256=context.manifest.sha256).path
            for stage in ("script_plan", "script_draft", "visual_binding"))
        before = _path_fingerprint(paths)
        plan, draft, binding = (reader(path) for reader, path in zip(
            (read_script_plan, read_script_draft, read_visual_binding), paths))
        try:
            if (not before or any(sha256_file(path) != getattr(context.artifacts, stage).sha256
                    for path, stage in zip(paths, ("script_plan", "script_draft", "visual_binding")))
                    or _path_fingerprint(paths) != before):
                raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "版本证据在读取期间已变化")
        except OSError as exc:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "版本证据在读取期间已变化") from exc
        with self._cache_lock:
            if len(self._stage_context_cache) >= 32:
                self._stage_context_cache.pop(next(iter(self._stage_context_cache)))
            self._stage_context_cache[cache_key] = (paths, before, deepcopy((plan, draft, binding)))
        return project, context, plan, draft, binding

    def _package_media(self, project: dict, brief: dict, package_evidence: dict | None = None):
        # Frozen versions own their category and sources, even after the project changes type.
        category = category_id_for_script_type(brief["脚本类型"]) if brief.get("脚本类型") else project["category_id"]
        scoped_project = {**project, "category_id": category}
        if package_evidence is not None:
            bindings = list(package_evidence.get("library_bindings", []))
        else:
            bindings = []
            if self.package_registry is not None:
                registered = self.package_registry.list()
                for library_category, aliases in sorted(registered["aliases"].items()):
                    current_ref = aliases.get("formal")
                    if (not current_ref or current_ref == project.get("template_package_ref")
                            or library_category == category and not project.get('template_package_ref')):
                        continue
                    # _single_package_media resolves and verifies this frozen identity.
                    # Do not traverse every shared package twice on one page request.
                    record = registered["packages"].get(current_ref) or {}
                    bindings.append({"resolved_ref": current_ref, "category_id": library_category,
                        **{key: record.get(key) for key in ("package_id", "package_version", "package_sha256")}})
        sources = []
        loaded = []
        for binding in [package_evidence, *bindings]:
            source_category = binding.get("category_id", category) if binding else category
            package, media = self._single_package_media({**scoped_project, "category_id": source_category}, brief, binding, allow_empty=bool(bindings))
            source = {"package_id": package["package_id"], "package_version": int(package.get("package_version") or 1),
                "category_id": source_category, "package_manifest_sha256": package["verified_manifest_sha256"]}
            sources.append(source)
            loaded.append((package, media, source))
        source_scope_sha256 = sha256_json(sources)
        cache_key = (source_scope_sha256, tuple(
            (package.get("verified_package_sha256"), package.get("media_snapshot_sha256") or sha256_json(media))
            for package, media, _ in loaded))
        # Every package and its current file fingerprint was checked above. Only
        # the immutable merge is reused; permissions and project locks are not cached.
        with self._cache_lock:
            cached = self._combined_media_cache.get(cache_key)
        if cached is not None:
            return cached
        combined = {}
        for package, media, source in loaded:
            for value in media:
                item = deepcopy(value)
                item["package_sources"] = [{**source, "media_id": item["media_id"],
                    "sources": _public_sources(item.get("source_links") or [], item.get("source") or {})}]
                previous = combined.get(item["content_sha256"])
                if previous is None:
                    combined[item["content_sha256"]] = item
                else:
                    previous["package_sources"].extend(item["package_sources"])
                    for link in item.get("source_links", []):
                        if link not in previous["source_links"]:
                            previous["source_links"].append(link)
                    previous["source_semantic_text"] = " ".join(dict.fromkeys(
                        [previous.get("source_semantic_text", ""), item.get("source_semantic_text", "")]))
        result = ({**loaded[0][0], "source_packages": sources, "source_scope_sha256": source_scope_sha256},
                  list(combined.values()))
        with self._cache_lock:
            if len(self._combined_media_cache) >= 8:
                self._combined_media_cache.pop(next(iter(self._combined_media_cache)))
            self._combined_media_cache[cache_key] = result
        return result

    def _single_package_media(self, project: dict, brief: dict, package_evidence: dict | None = None,
                              *, allow_empty: bool = False):
        try:
            binding = package_evidence or (project if project.get("template_package_ref") else None)
            if binding and self.package_registry is not None:
                resolved = self.package_registry.resolve_evidence(project["category_id"], binding)
            else:
                resolved = self.asset_package_resolver(str(brief.get("脚本类型") or ""))
        except (AssetPackageError, PackageRegistryError, OSError, TypeError, ValueError) as exc:
            raise WorkspaceServiceError(
                "ASSET_PACKAGE_RESOLVER_INVALID", "资产包解析失败",
            ) from exc
        if resolved is None:
            raise WorkspaceServiceError("ASSET_PACKAGE_UNAVAILABLE", "当前类别没有锁定资产包")
        package_path, resolved_record = self._resolved_package_path(resolved)
        lock = self._package_media_load_locks[hash(str(package_path)) % len(self._package_media_load_locks)]
        with lock:
            return self._read_package_media(project, resolved, package_path, resolved_record, allow_empty=allow_empty)

    def _read_package_media(self, project: dict, resolved, package_path, resolved_record,
                            *, allow_empty: bool):
        # Coalesce simultaneous cold pages and previews before reading the index
        # or preparing pixels. Registry integrity was checked before this lock.
        try:
            package = load_asset_package(package_path)
        except (AssetPackageError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ASSET_PACKAGE_INVALID", "类别资产包无法读取") from exc
        if resolved_record is not None and (
            str(package.get("package_id") or "") != resolved_record["package_id"]
            or int(package.get("package_version") or 0) != resolved_record["package_version"]
            or str(package.get("category_id") or "") != resolved_record["category_id"]
        ):
            raise WorkspaceServiceError("ASSET_PACKAGE_DRIFT", "资产包解析结果与锁定版本不一致")
        if package.get("category_id") != project["category_id"]:
            raise WorkspaceServiceError("ASSET_PACKAGE_CATEGORY_MISMATCH", "资产包类别与项目不一致")
        cache_key = str(Path(package["manifest_path"]).resolve())
        cached = self._package_media_cache.get(cache_key)
        if cached is not None:
            fingerprint = _package_media_fingerprint(package, cached[2])
            if fingerprint == cached[0]:
                if (
                    resolved_record is not None
                    and cached[1]["verified_manifest_sha256"] != resolved_record["manifest_sha256"]
                ):
                    raise WorkspaceServiceError("ASSET_PACKAGE_DRIFT", "资产包 manifest SHA-256 漂移")
                return cached[1], cached[2]
        verified_manifest_sha256 = sha256_file(package["manifest_path"])
        if (
            resolved_record is not None
            and verified_manifest_sha256 != resolved_record["manifest_sha256"]
        ):
            raise WorkspaceServiceError("ASSET_PACKAGE_DRIFT", "资产包 manifest SHA-256 漂移")
        index = Path(package["index_path"])
        if not index.is_file():
            raise WorkspaceServiceError("ASSET_PACKAGE_INVALID", "资产包索引缺失")
        expected_index_sha = str(package.get("index_sha256") or "")
        if expected_index_sha and sha256_file(index) != expected_index_sha:
            raise WorkspaceServiceError("ASSET_PACKAGE_DRIFT", "资产包索引 SHA-256 漂移")
        blob_records = self._verified_blob_records(package)
        persistent_pixels = self.package_registry is not None and isinstance(resolved, Mapping)
        pixel_evidence = (
            load_package_pixel_evidence(self.package_registry.path, dict(resolved))
            if persistent_pixels else {}
        )
        updated_pixels = {}
        connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT rowid AS package_source_row, * FROM media_assets WHERE package_id=? ORDER BY media_id",
                (package["package_id"],),
            ).fetchall()
            source_payloads = {}
            for item in connection.execute(
                """SELECT source_file, source_sheet, source_row, payload_json
                   FROM script_assets WHERE package_id=? ORDER BY rowid""", (package["package_id"],),
            ):
                source_payloads.setdefault((item["source_file"], item["source_sheet"], item["source_row"]), item["payload_json"])
            source_semantics = {}
            links_by_media = {}
            for item in connection.execute(
                """SELECT l.media_id, l.reference_kind, l.source_file, l.source_sheet, l.source_row,
                          l.structure, l.shot_no, l.visual_desc, l.shot_function,
                          l.visual_subject, l.tags, l.source_brand
                   FROM media_script_links l JOIN media_assets m ON m.media_id=l.media_id
                   WHERE m.package_id=?
                   ORDER BY l.media_id, l.source_sheet, l.source_row, l.reference_kind""", (package["package_id"],),
            ):
                link = dict(item)
                media_id = link.pop("media_id")
                source_key = (link["source_file"], link["source_sheet"], link["source_row"])
                if source_key in source_payloads:
                    if source_key not in source_semantics:
                        source_semantics[source_key] = json.loads(source_payloads[source_key])
                    semantics = source_semantics[source_key]
                    link.update({key: semantics[key] for key in ('generic_desc', 'brand_judgement_original',
                        'sheet_overview', 'semantic_source_anchors', 'source_semantics_version') if key in semantics})
                links_by_media.setdefault(media_id, []).append(link)
            result = []
            for row in rows:
                value = dict(row)
                path = Path(str(value["current_path"]))
                if not path.is_absolute():
                    path = Path(package["package_root"]) / path
                digest = str(value["content_sha256"])
                verified_digest = (
                    self.package_registry.verified_file_sha256(dict(resolved), path)
                    if self.package_registry is not None and isinstance(resolved, Mapping)
                    else None
                )
                if not path.is_file() or (verified_digest or sha256_file(path)) != digest:
                    raise WorkspaceServiceError("ASSET_PACKAGE_BLOB_DRIFT", "资产包媒体缺失或 SHA-256 漂移")
                declared_blob = blob_records.get(str(value["media_id"]))
                if blob_records and declared_blob is None:
                    raise WorkspaceServiceError("ASSET_PACKAGE_BLOB_DRIFT", "资产包媒体未登记到 blob manifest")
                if declared_blob is not None and (
                    declared_blob.get("content_sha256") != digest
                    or declared_blob.get("path") != str(value["current_path"])
                ):
                    raise WorkspaceServiceError("ASSET_PACKAGE_BLOB_DRIFT", "资产包媒体与 blob manifest 不一致")
                links = links_by_media.get(value["media_id"], [])
                source = links[0] if links else {
                    "source_file": str(value["current_path"]),
                    "source_sheet": "media_assets",
                    "source_row": int(value["package_source_row"]),
                }
                analysis = {}
                if value.get("analysis_json"):
                    try:
                        analysis = json.loads(str(value["analysis_json"]))
                    except json.JSONDecodeError as exc:
                        raise WorkspaceServiceError("ASSET_PACKAGE_INVALID", "资产包图片分析无效") from exc
                analysis = normalize_visual_analysis(analysis)
                # Legacy visual rows may omit an embedded image hash. Their
                # immutable index row explicitly binds analysis_json to this
                # media_id and content hash; both index and pixels were verified
                # above. Project that binding without rewriting the frozen package
                # or overriding an explicit (possibly conflicting) analysis hash.
                if (analysis and expected_index_sha and declared_blob is not None
                        and not analysis.get("content_sha256") and not analysis.get("image_sha256")):
                    analysis = {**analysis, "content_sha256": digest,
                        "evidence_binding": {"kind": "verified_package_index_row",
                            "media_id": str(value["media_id"]), "index_sha256": expected_index_sha,
                            "package_manifest_sha256": verified_manifest_sha256}}
                tags = _strings(analysis.get("tags") or ["类别默认图"])
                caption = str(
                    analysis.get("caption") or "画面待识别"
                ).strip()
                source_semantic_text = " ".join(
                    " ".join(str(link.get(key) or "") for key in (
                        "structure", "generic_desc", "visual_desc", "shot_function", "visual_subject", "tags",
                    ))
                    for link in links
                ).strip()
                if persistent_pixels:
                    try:
                        similarity, stored = package_image_similarity(path, pixel_evidence)
                    except (OSError, ValueError):
                        # Optional eager caching must not reject a previously readable header.
                        with Image.open(path) as image:
                            is_animated = bool(getattr(image, "is_animated", False))
                    else:
                        updated_pixels[str(path.resolve())] = stored
                        is_animated = similarity["frames"] > 1
                else:
                    with Image.open(path) as image:
                        is_animated = bool(getattr(image, "is_animated", False))
                result.append({
                    "media_id": str(value["media_id"]), "content_sha256": digest,
                    "analysis_state": ("failed" if value.get("review_status") in {"failed", "error"}
                                       else "available" if value.get("analysis_json") else "pending"),
                    "path": str(path.resolve()), "caption": caption,
                    "is_animated": is_animated,
                    "tags": tags, "subjects": list(analysis.get("subjects") or []),
                    "main_subject": str(analysis.get("main_subject") or ""),
                    "foreground_subjects": list(analysis.get("foreground_subjects") or []),
                    "background_subjects": list(analysis.get("background_subjects") or []),
                    "scene": str(analysis.get("scene") or analysis.get("scene_type") or "场景待识别"),
                    "scene_type": str(analysis.get("scene_type") or analysis.get("scene") or ""),
                    "shot_type": str(analysis.get("shot_type") or ""),
                    "action_type": str(analysis.get("action_type") or ""),
                    "camera_angle": str(analysis.get("camera_angle") or ""),
                    "camera_movement": str(analysis.get("camera_movement") or ""),
                    "quality": _quality_summary(analysis),
                    "quality_flags": list(analysis.get("quality_flags") or []),
                    "uncertainties": list(analysis.get("uncertainties") or []),
                    "brand": _brand_label(analysis, source),
                    "purpose": str(analysis.get("purpose") or source.get("shot_function") or ""),
                    "negative_flags": list(analysis.get("negative_flags") or []),
                    "ocr_text": str(analysis.get("ocr_text") or value.get("ocr_text") or ""),
                    "visual_analysis": analysis,
                    "source_semantic_text": source_semantic_text,
                    "source_links": links,
                    "searchable": " ".join(str(value.get(key) or "") for key in (
                        "filename", "searchable_text", "caption", "tags_json", "ocr_text",
                    )).casefold() + " " + " ".join(str(item.get("visual_desc") or "") for item in links).casefold(),
                    "source": {
                        "file": str(source.get("source_file") or package["package_id"]),
                        "sheet": str(source.get("source_sheet") or "media_assets"),
                        "row": int(source.get("source_row") or 1),
                    },
                })
        finally:
            connection.close()
        if not result and not allow_empty:
            raise WorkspaceServiceError("ASSET_PACKAGE_MEDIA_EMPTY", "类别资产包没有可用图片")
        verified_package = {
            **package,
            "verified_manifest_sha256": verified_manifest_sha256,
            "verified_package_sha256": resolved.get("package_sha256") if isinstance(resolved, Mapping) else None,
            "media_snapshot_sha256": sha256_json(result),
        }
        fingerprint = _package_media_fingerprint(verified_package, result)
        if persistent_pixels and updated_pixels != pixel_evidence:
            save_package_pixel_evidence(self.package_registry.path, dict(resolved), updated_pixels)
        self._package_media_cache[cache_key] = (fingerprint, verified_package, result)
        return verified_package, result

    @staticmethod
    def _resolved_package_path(
        resolved: str | Path | Mapping,
    ) -> tuple[str | Path, dict | None]:
        if not isinstance(resolved, Mapping):
            if not isinstance(resolved, (str, Path)):
                raise WorkspaceServiceError(
                    "ASSET_PACKAGE_RESOLVER_INVALID", "资产包解析结果类型无效",
                )
            return resolved, None
        record = dict(resolved)
        local_path = record.get("local_path")
        package_id = str(record.get("package_id") or "").strip()
        package_version = record.get("package_version")
        category_id = str(record.get("category_id") or "").strip()
        manifest_sha256 = str(record.get("manifest_sha256") or "").strip().casefold()
        package_sha256 = str(record.get("package_sha256") or "").strip().casefold()
        alias = str(record.get("alias") or "").strip()
        resolved_ref = str(record.get("resolved_ref") or "").strip()
        if (
            not isinstance(local_path, (str, Path))
            or not str(local_path).strip()
            or not package_id
            or type(package_version) is not int
            or package_version < 1
            or not category_id
            or not _sha256_value(manifest_sha256)
            or not _sha256_value(package_sha256)
            or alias not in {"formal", "configured", "legacy-configured", "project"}
            or resolved_ref != f"{package_id}@{package_version}"
            or not isinstance(record.get("files"), list)
            or any(not isinstance(item, Mapping) for item in record.get("files") or [])
        ):
            raise WorkspaceServiceError(
                "ASSET_PACKAGE_RESOLVER_INVALID", "资产包解析结果缺少已锁定的身份或来源",
            )
        try:
            current_sha256 = package_record_sha256(record)
        except (AttributeError, TypeError, ValueError) as exc:
            raise WorkspaceServiceError(
                "ASSET_PACKAGE_RESOLVER_INVALID", "资产包解析记录无法校验",
            ) from exc
        if current_sha256 != package_sha256:
            raise WorkspaceServiceError(
                "ASSET_PACKAGE_DRIFT", "资产包解析记录 SHA-256 已漂移",
            )
        return local_path, {
            "package_id": package_id,
            "package_version": package_version,
            "category_id": category_id,
            "manifest_sha256": manifest_sha256,
        }

    @staticmethod
    def _verified_blob_records(package: dict) -> dict[str, dict]:
        relative = str(package.get("blob_manifest") or "").strip()
        if not relative:
            return {}
        path = (Path(package["package_root"]) / relative).resolve()
        try:
            root = Path(package["package_root"]).resolve()
            path.relative_to(root)
        except ValueError as exc:
            raise WorkspaceServiceError("ASSET_PACKAGE_INVALID", "blob manifest 路径越界") from exc
        expected = str(package.get("blob_manifest_sha256") or "")
        if not path.is_file() or (expected and sha256_file(path) != expected):
            raise WorkspaceServiceError("ASSET_PACKAGE_DRIFT", "资产包 blob manifest 缺失或 SHA-256 漂移")
        try:
            rows = [
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ASSET_PACKAGE_INVALID", "资产包 blob manifest 无法读取") from exc
        return {str(item.get("media_id") or ""): item for item in rows}

    @staticmethod
    def _public_candidate(
        item: dict, project_id: str, media: list[dict], candidate_pool: list[dict] | None = None,
    ) -> dict:
        source_item = next(value for value in media if value["media_id"] == item["image_id"])
        links = list(source_item.get("source_links") or [])
        sources = _public_sources(links, source_item.get("source") or {})
        source_category = _source_category(sources)
        source_packages = list(source_item.get('package_sources') or [])
        if source_packages:
            source_category = '、'.join(dict.fromkeys(metadata_for_category_id(item['category_id']).display_name
                for item in source_packages if item.get('category_id')))
        hits = list(item.get("hits") or [])
        missing = list(item.get("missing") or [])
        conflicts = list(item.get("conflicts") or [])
        negative_flags = list(item.get("negative_flags") or [])
        cluster_id = str(item.get("perceptual_cluster_id") or "")
        near_duplicate_count = len({
            str(value.get("image_id") or "")
            for value in (candidate_pool or [])
            if cluster_id
            and str(value.get("perceptual_cluster_id") or "") == cluster_id
            and str(value.get("image_id") or "") != str(item.get("image_id") or "")
        })
        near_duplicate = (
            f"候选池另有 {near_duplicate_count} 张近重复画面，采用前需核对"
            if near_duplicate_count else
            "当前候选池未发现近重复画面"
            if cluster_id else
            "类别浏览阶段不判定视觉簇；进入逐镜匹配后复核近重复画面"
        )
        explanation = build_match_explanation(
            item, sources=sources, source_category=source_category,
            shot_functions=tuple(
                str(link.get("shot_function") or "")
                for link in source_item.get("source_links") or []
            ),
            reuse="多镜选择同一候选时，镜头级复用提示会显示采用次数",
            near_duplicate=near_duplicate,
        )
        return {
            "candidate_key": item["image_id"], "media_id": item["image_id"],
            "duplicate_group_id": str(item.get("duplicate_group_id") or ""),
            "duplicate_of": str(item.get("duplicate_of") or ""),
            "duplicate_count": int(item.get("duplicate_count") or 0),
            "content_sha256": source_item["content_sha256"],
            "is_animated": bool(source_item.get("is_animated")),
            "caption": item["summary"], "tags": item["tags"], "source": sources[0],
            "source_category": source_category, "sources": sources,
            'package_sources': source_packages,
            "source_status": "script_row" if links else "trusted_package",
            "source_status_label": "有脚本行来源" if links else "仅有可信包内素材来源",
            "analysis_state": source_item.get("analysis_state", "available"),
            "score_explanation": "排序分，仅用于候选排序，不是准确率或质量认证",
            "visual_evidence": {
                "kind": item.get("evidence_kind") or "",
                "ocr_text": item.get("ocr_text") or "",
                "negative_flags": negative_flags,
                "main_subject": source_item.get("main_subject") or "",
                "background_subjects": list(source_item.get("background_subjects") or []),
                "scene": source_item.get("scene") or "",
                "shot_type": source_item.get("shot_type") or "",
                "action_type": source_item.get("action_type") or "",
                "camera_angle": source_item.get("camera_angle") or "",
                "quality": source_item.get("quality") or "画质待核对",
                "quality_flags": list(source_item.get("quality_flags") or []),
                "uncertainties": list(source_item.get("uncertainties") or []),
            },
            "template_recommendations": [
                {
                    "shot_no": str(link.get("shot_no") or ""),
                    "structure": str(link.get("structure") or ""),
                    "shot_function": str(link.get("shot_function") or ""),
                    "source": _public_source(link),
                }
                for link in source_item.get("source_links") or []
            ],
            "score": item["score"], "match_basis": item["match_reason"],
            "match_tier": item.get("match_tier") or "",
            "coverage_signature": item.get("coverage_signature") or "",
            "default_eligible": bool(item.get("default_eligible", True)),
            "suggestion_eligible": bool(item.get("suggestion_eligible")),
            "suggestion_kind": item.get("suggestion_kind", "composition_reference"),
            "suggestion_reason": item.get("suggestion_reason", ""),
            "visual_evidence_bound": bool(item.get("visual_evidence_bound")),
            "hits": hits, "missing": missing,
            "conflicts": conflicts, "brand_warning": item.get("brand_warning") or "",
            "requires_human_review": bool(item.get("requires_human_review")),
            "rank_components": item.get("rank_components") or {},
            "match_explanation": explanation,
            "preview_url": f"/api/formal/projects/{project_id}/images/default-candidates/{item['image_id']}/preview",
        }


def _sha256_value(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _public_source(value: Mapping) -> dict:
    source_file = str(value.get("source_file") or value.get("file") or "").strip()
    if "/" in source_file or "\\" in source_file:
        source_file = source_file.replace("\\", "/").rsplit("/", 1)[-1]
    return {
        "file": source_file,
        "sheet": str(value.get("source_sheet") or value.get("sheet") or "").strip(),
        "row": int(value.get("source_row") or value.get("row") or 1),
    }


def _public_sources(links: list[dict], fallback: Mapping) -> list[dict]:
    raw = [_public_source(item) for item in links] or [_public_source(fallback)]
    unique: list[dict] = []
    seen: set[tuple[str, str, int]] = set()
    for source in raw:
        identity = (source["file"], source["sheet"], source["row"])
        if identity not in seen:
            seen.add(identity)
            unique.append(source)
    return unique


def _source_category(sources: list[dict]) -> str:
    sheets = " ".join(str(item.get("sheet") or "").casefold() for item in sources)
    if "创意" in sheets or "creative" in sheets:
        return "创意库"
    if "模板" in sheets or "模版" in sheets or "template" in sheets:
        return "模板"
    return "资产包"


def _media_facets(item: dict) -> dict[str, list[str]]:
    def values(raw):
        raw = raw if isinstance(raw, (list, tuple)) else [raw]
        result = [str(value).strip() for value in raw if str(value or "").strip()
                  and str(value).strip() not in {"未识别", "场景待识别", "画面待识别", "未知"}]
        return sorted(set(result)) or ["__unlabeled__"]

    links = item.get("source_links") or []
    categories = {_source_category([_public_source(link)]) for link in links}
    return {
        "source": sorted({{"创意库": "creative", "模板": "template"}.get(category, "package") for category in categories}) or ["package"],
        "scene": values(item.get("scene_type") or item.get("scene")),
        "subject": values([item.get("main_subject"), *(item.get("subjects") or [])]),
        "brand": values(item.get("brand")),
        "framing": values(item.get("shot_type")),
        "analysis_status": values("ready" if item.get("analysis_state") == "available" else item.get("analysis_state")),
    }


def _candidate_facets(candidates: list[dict], media: list[dict]) -> dict:
    by_id = {item["media_id"]: item for item in media}
    counts = {key: {} for key in ("source", "scene", "subject", "brand", "framing", "analysis_status")}
    for candidate in candidates:
        group = str(candidate.get("duplicate_group_id") or candidate["image_id"])
        for key, values in _media_facets(by_id[candidate["image_id"]]).items():
            for value in values:
                counts[key].setdefault(value, set()).add(group)
    labels = {"__unlabeled__": "未标注", "creative": "创意库", "template": "模板参考",
              "package": "包内素材", "ready": "已有分析", "pending": "待分析", "failed": "分析失败"}
    return {key: [{"value": value, "label": labels.get(value, value), "count": len(groups)}
                  for value, groups in sorted(options.items())] for key, options in counts.items()}


def _filter_candidates(candidates: list[dict], media: list[dict], query: str, filters: dict) -> list[dict]:
    by_id = {item["media_id"]: item for item in media}
    terms = query.casefold().split()
    result = []
    for candidate in candidates:
        item = by_id[candidate["image_id"]]
        searchable = " ".join(str(value or "") for value in (
            item.get("caption"), item.get("scene"), item.get("purpose"), item.get("brand"),
            item.get("main_subject"), item.get("ocr_text"), item.get("source_semantic_text"),
            *item.get("subjects", ()), *item.get("tags", ()),
            *item.get("foreground_subjects", ()), *item.get("background_subjects", ()),
        )).casefold()
        if not all(term in searchable for term in terms):
            continue
        facets = _media_facets(item)
        if any(value and value not in facets[key] for key, value in filters.items()):
            continue
        result.append(dict(candidate))
    return result


def _limit_candidate_groups(candidates: list[dict], limit: int | None) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for item in candidates:
        group = str(item.get("duplicate_group_id") or item["image_id"])
        if limit is not None and group not in groups and len(groups) >= limit:
            continue
        groups.setdefault(group, []).append(dict(item))
    normalized = {}
    for values in groups.values():
        for index, item in enumerate(values):
            item["duplicate_of"] = values[0]["image_id"] if index else ""
            item["duplicate_count"] = len(values) - 1
            normalized[item["image_id"]] = item
    return [normalized[item["image_id"]] for item in candidates if item["image_id"] in normalized]


def _aggregated_gap_reason(result: dict) -> str:
    base = str(result.get("gap_reason") or "没有安全图片候选")
    counts = Counter(
        str(item.get("reason") or "").strip()
        for item in result.get("rejected") or []
        if str(item.get("reason") or "").strip()
    )
    if not counts:
        return base
    reasons = "；".join(
        f"{reason}（{count} 项）" for reason, count in counts.most_common(3)
    )
    return f"{base}；主要拒绝原因：{reasons}"


def _path_fingerprint(paths) -> tuple:
    try:
        return tuple((str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size,
                      stat.st_mtime_ns, stat.st_ctime_ns)
                     for path in paths for stat in (path.stat(),))
    except OSError:
        return ()


def _package_media_fingerprint(package: dict, media: list[dict]) -> tuple[tuple, ...]:
    """Detect package changes cheaply after one complete SHA verification."""
    paths: dict[str, tuple[str, Path]] = {
        "manifest": ("ASSET_PACKAGE_DRIFT", Path(package["manifest_path"])),
        "index": ("ASSET_PACKAGE_DRIFT", Path(package["index_path"])),
    }
    relative_blob_manifest = str(package.get("blob_manifest") or "").strip()
    if relative_blob_manifest:
        paths["blob_manifest"] = (
            "ASSET_PACKAGE_DRIFT",
            (Path(package["package_root"]) / relative_blob_manifest).resolve(),
        )
    for item in media:
        paths[f"media:{item['media_id']}"] = (
            "ASSET_PACKAGE_BLOB_DRIFT", Path(item["path"]),
        )
    fingerprint = []
    for label, (error_code, path) in sorted(paths.items()):
        try:
            info = path.stat()
        except OSError as exc:
            message = "资产包媒体缺失或 SHA-256 漂移" if error_code == "ASSET_PACKAGE_BLOB_DRIFT" else "资产包索引或清单缺失"
            raise WorkspaceServiceError(error_code, message) from exc
        if not stat.S_ISREG(info.st_mode):
            message = "资产包媒体缺失或 SHA-256 漂移" if error_code == "ASSET_PACKAGE_BLOB_DRIFT" else "资产包索引或清单缺失"
            raise WorkspaceServiceError(error_code, message)
        fingerprint.append((
            label,
            # These paths were resolved when the manifest/index/media were loaded.
            # Device and inode still detect a changed target without rewalking every
            # ancestor for every thumbnail in a page.
            str(path),
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ))
    return tuple(fingerprint)


def _strings(value) -> list[str]:
    if isinstance(value, str):
        raw = value.replace("，", ",").replace("、", ",").split(",")
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    result = [str(item).strip() for item in raw if str(item).strip()]
    return result or ["类别默认图"]


def _quality_summary(analysis: dict) -> str:
    if str(analysis.get("quality") or "").strip():
        return str(analysis["quality"]).strip()
    labels = {
        "slight_blur": "轻微模糊", "severe_blur": "严重模糊",
        "out_of_focus": "虚焦", "low_resolution": "分辨率较低",
        "overexposed": "过曝", "underexposed": "欠曝", "bad_crop": "裁切不完整",
        "watermark": "有水印", "unreadable_subject": "主体难以辨认",
    }
    flags = list(analysis.get("quality_flags") or [])
    return "、".join(labels.get(flag, flag) for flag in flags[:3]) or "画质待核对"


def _brand_label(analysis: dict, source: dict) -> str:
    value = (
        analysis.get("brand")
        or analysis.get("brand_names")
        or source.get("source_brand")
        or ""
    )
    if isinstance(value, (list, tuple)):
        labels = [str(item).strip() for item in value if str(item).strip()]
        return "、".join(labels) if labels else "未识别"
    return str(value).strip() or "未识别"


def _image_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    raise WorkspaceServiceError("ASSET_PACKAGE_MEDIA_INVALID", "默认图片格式不受支持")
