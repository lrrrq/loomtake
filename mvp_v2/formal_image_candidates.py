"""Formal projection into the existing authoritative image retrieval engine."""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
from functools import lru_cache
import hmac
from io import BytesIO
import json
from pathlib import Path
from threading import Lock

from PIL import Image, ImageChops, ImageOps, ImageStat

from .image_retrieval import search_images_for_slot, assess_image_candidates
from .image_understanding import normalize_visual_analysis, normalize_visual_flags
from .package_registry import _verification_cache_root, _verification_key, _receipt_signature
from .run_contract import sha256_json
from .run_state import write_json_atomic


PERCEPTUAL_HASH_DISTANCE = 6
_RESTORED_PIXEL_EVIDENCE: OrderedDict[tuple, dict] = OrderedDict()
_PIXEL_EVIDENCE_LOCK = Lock()


def _pixel_file_key(path: Path) -> tuple:
    info = path.stat()
    return (str(path.resolve()), info.st_dev, info.st_ino, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _pixel_cache_identity(record: dict) -> dict:
    return {"schema": "mplus-image-pixels/v1", "pillow": Image.__version__,
            "package_sha256": record["package_sha256"],
            "local_path": str(Path(record["local_path"]).resolve())}


def load_package_pixel_evidence(registry_path: Path, record: dict) -> dict:
    """Read disposable, authenticated pixel results after registry verification."""
    root = _verification_cache_root(registry_path, Path(record["local_path"]))
    key = _verification_key(root, create=False) if root is not None else None
    if key is None:
        return {}
    identity = _pixel_cache_identity(record)
    path = root / ("pixels-" + sha256_json(identity) + ".json")
    try:
        if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        receipt = payload["receipt"]
        if (receipt["identity"] == identity and isinstance(receipt["pixels"], dict)
                and hmac.compare_digest(str(payload.get("signature") or ""),
                                        _receipt_signature(key, receipt))):
            return receipt["pixels"]
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return {}


def package_image_similarity(path: Path, persisted: dict) -> tuple[dict, dict]:
    """Match signed evidence to the current file before warming the canonical cache."""
    file_key = _pixel_file_key(path)
    stored = persisted.get(file_key[0]) or {}
    if stored.get("file_key") == list(file_key):
        value = {**stored["value"], "color": tuple(stored["value"]["color"])}
        with _PIXEL_EVIDENCE_LOCK:
            _RESTORED_PIXEL_EVIDENCE[file_key] = value
            _RESTORED_PIXEL_EVIDENCE.move_to_end(file_key)
            while len(_RESTORED_PIXEL_EVIDENCE) > 2048:
                _RESTORED_PIXEL_EVIDENCE.popitem(last=False)
    else:
        value = _image_similarity({"path": str(path)})
    return value, {"file_key": list(file_key), "value": {**value, "color": list(value["color"])}}


def save_package_pixel_evidence(registry_path: Path, record: dict, pixels: dict) -> None:
    root = _verification_cache_root(registry_path, Path(record["local_path"]))
    key = _verification_key(root, create=True) if root is not None else None
    if key is None:
        return
    identity = _pixel_cache_identity(record)
    receipt = {"identity": identity, "pixels": pixels}
    try:
        write_json_atomic(root / ("pixels-" + sha256_json(identity) + ".json"), {
            "receipt": receipt, "signature": _receipt_signature(key, receipt),
        })
    except OSError:
        pass


def build_match_explanation(
    match: dict,
    *,
    sources: list[dict],
    source_category: str,
    shot_functions: list[str] | tuple[str, ...] = (),
    reuse: str = "选择后会显示当前版本的复用次数",
    near_duplicate: str = "进入逐镜选择时复核近重复画面",
) -> dict:
    """Format canonical matcher evidence for employee-facing DTOs."""
    hits = list(match.get("hits") or [])
    missing = list(match.get("missing") or [])
    conflicts = list(match.get("conflicts") or [])
    negative_flags = list(match.get("negative_flags") or [])
    semantic = str(match.get("match_reason") or "").strip() or "权威图片检索已返回该候选"
    warnings = [
        str(value).strip() for value in (
            *conflicts, *negative_flags, match.get("brand_warning") or "",
        ) if str(value).strip()
    ]
    if warnings:
        safety = "需人工核对：" + "；".join(warnings)
    elif match.get("requires_human_review"):
        safety = str(match.get("review_label") or "候选通过硬性安全过滤，仍需人工看图确认")
    else:
        safety = "候选通过权威检索的硬性安全过滤"
    return {
        "total_score": int(match.get("score") or 0),
        "requirements": list(match.get("requirements") or []),
        "reference_notes": list(match.get("reference_notes") or []),
        "components": dict(match.get("rank_components") or {}),
        "hits": hits,
        "missing": missing,
        "conflicts": conflicts,
        "source_category": str(source_category or "项目素材"),
        "sources": list(sources),
        "semantic": semantic,
        "safety": safety,
        "shot_functions": list(dict.fromkeys(
            str(value).strip() for value in shot_functions if str(value).strip()
        )),
        "reuse": reuse,
        "near_duplicate": near_duplicate,
        "recommendation": semantic,
    }


def perceptual_cluster_ids(items: list[dict]) -> dict[str, str]:
    """Bound every pair in a group; a similar intermediary cannot merge two scenes."""
    evidence = {str(item["image_id"]): _image_similarity(item) for item in items}
    groups: list[list[str]] = []

    def similar(left: str, right: str) -> bool:
        first, second = evidence[left], evidence[right]
        # A matching first frame says nothing about the rest of an animation.
        if first["frames"] > 1 or second["frames"] > 1:
            return False
        if (first["hash"] ^ second["hash"]).bit_count() > PERCEPTUAL_HASH_DISTANCE:
            return False
        if first["color"] and second["color"]:
            return sum(abs(a - b) for a, b in zip(first["color"], second["color"])) / 3 <= 70
        return True

    for image_id in sorted(evidence):
        group = next((values for values in groups if all(similar(image_id, other) for other in values)), None)
        if group is None:
            groups.append([image_id])
        else:
            group.append(image_id)
    result = {}
    for group in groups:
        cluster = "visual_" + sha256("\n".join(group).encode("utf-8")).hexdigest()[:16]
        result.update({image_id: cluster for image_id in group})
    return result


def allocate_diverse_defaults(
    ranked_by_shot: dict[str, dict],
    *,
    pinned_by_shot: dict[str, str] | None = None,
) -> dict:
    """Choose only from canonical candidates, preferring an unused visual cluster."""
    pinned = dict(pinned_by_shot or {})
    selected: dict[str, str] = {}
    used_clusters: dict[str, int] = {}
    reuse_required: dict[str, str] = {}
    candidate_by_shot = {
        shot_no: list(result.get("candidates") or [])
        for shot_no, result in ranked_by_shot.items()
    }
    for shot_no, image_id in pinned.items():
        candidate = next((
            item for item in candidate_by_shot.get(shot_no, [])
            if str(item.get("image_id") or "") == image_id
        ), None)
        if candidate is None:
            continue
        cluster = str(candidate.get("perceptual_cluster_id") or image_id)
        selected[shot_no] = image_id
        used_clusters[cluster] = used_clusters.get(cluster, 0) + 1
    pending = [shot_no for shot_no in candidate_by_shot if shot_no not in selected]
    pending.sort(key=lambda shot_no: (
        len({
            str(item.get("perceptual_cluster_id") or item.get("image_id") or "")
            for item in candidate_by_shot[shot_no]
        }),
        _shot_sort_key(shot_no),
    ))
    for shot_no in pending:
        candidates = candidate_by_shot[shot_no]
        if not candidates:
            continue
        recommended = [item for item in candidates if item.get("default_eligible", True)]
        if not recommended:
            continue
        best = max(recommended, key=lambda item: int(item.get("score") or 0))
        best_score = int(best.get("score") or 0)
        # A narrow score band only compares the same current content coverage.
        equivalent = [item for item in recommended if
            best_score - int(item.get("score") or 0) <= (5 if best.get("coverage_signature") else 0)
            and item.get("match_tier") == best.get("match_tier")
            and item.get("coverage_signature") == best.get("coverage_signature")]
        equivalent.sort(key=lambda item: -int(item.get("score") or 0))
        candidate = next((
            item for item in equivalent
            if str(item.get("perceptual_cluster_id") or item.get("image_id") or "")
            not in used_clusters
        ), equivalent[0])
        image_id = str(candidate.get("image_id") or "")
        cluster = str(candidate.get("perceptual_cluster_id") or image_id)
        if cluster in used_clusters:
            reuse_required[shot_no] = cluster
        selected[shot_no] = image_id
        used_clusters[cluster] = used_clusters.get(cluster, 0) + 1
    return {
        "selected_by_shot": selected,
        "reuse_required_by_shot": reuse_required,
        "cluster_counts": used_clusters,
    }


def _shot_sort_key(value: str) -> tuple[int, str]:
    try:
        return int(value), value
    except (TypeError, ValueError):
        return 10**9, str(value)


def _perceptual_hash_value(item: dict) -> int:
    return _image_similarity(item)["hash"]


def _image_similarity(item: dict) -> dict:
    path = Path(item.get("path") or "")
    if path.is_file():
        file_key = _pixel_file_key(path)
        with _PIXEL_EVIDENCE_LOCK:
            restored = _RESTORED_PIXEL_EVIDENCE.get(file_key)
            if restored is not None:
                _RESTORED_PIXEL_EVIDENCE.move_to_end(file_key)
                return restored
        return _cached_image_similarity(file_key[0], file_key[1:])
    stored = str(item.get("perceptual_hash") or "")
    identity = str(item.get("content_sha256") or item.get("image_id") or "")
    try:
        value = int(stored, 16) if len(stored) == 16 else int(sha256(identity.encode()).hexdigest()[:16], 16)
    except ValueError:
        value = int(sha256(identity.encode()).hexdigest()[:16], 16)
    return {"hash": value, "static_key": "", "frames": 0, "color": ()}


@lru_cache(maxsize=2048)
def _cached_image_similarity(path: str, fingerprint: tuple) -> dict:
    del fingerprint
    # Decode in memory: PNG chunks and GIF frames otherwise cause many small disk reads.
    with open(path, "rb") as source:
        content = source.read()
    with Image.open(BytesIO(content)) as original:
        frames = int(getattr(original, "n_frames", 1))
        rgba = ImageOps.exif_transpose(original).convert("RGBA")
        static_key = sha256(
            f"{rgba.width}x{rgba.height}:RGBA:".encode() + rgba.tobytes()
        ).hexdigest() if frames == 1 else ""
        rgb = Image.new("RGB", rgba.size, "white")
        rgb.paste(rgba, mask=rgba.getchannel("A"))
        # Ignore a surrounding white document canvas for similarity, retaining the original asset.
        mask = ImageChops.difference(rgb, Image.new("RGB", rgb.size, "white")).convert("L")
        bounds = mask.point(lambda value: 255 if value > 20 else 0).getbbox()
        if bounds and (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]) >= rgb.width * rgb.height * .05:
            rgb = rgb.crop(bounds)
        pixels = rgb.convert("L").resize((9, 8), Image.Resampling.LANCZOS).tobytes()
        value = 0
        for y in range(8):
            for x in range(8):
                value = (value << 1) | int(pixels[y * 9 + x] > pixels[y * 9 + x + 1])
        return {"hash": value, "static_key": static_key, "frames": frames,
                "color": tuple(ImageStat.Stat(rgb.resize((8, 8))).mean)}


def _present_distinct_candidates(candidates: list[dict], media_by_id: dict[str, dict], top_k: int | None) -> list[dict]:
    """Fold only identical static pixels in presentation, preserving selectable identities."""
    groups: dict[str, list[dict]] = {}
    for candidate in candidates:
        image_id = str(candidate.get("image_id") or "")
        item = media_by_id.get(image_id, {})
        evidence = _image_similarity({**item, "image_id": image_id})
        group = evidence["static_key"] or image_id
        groups.setdefault(group, []).append(candidate)
    selected_groups = list(groups.items())[:None if top_k is None else max(1, top_k)]
    representatives, alternatives = [], []
    for group, values in selected_groups:
        representative = str(values[0]["image_id"])
        for index, value in enumerate(values):
            value["duplicate_group_id"] = "static_" + sha256(group.encode()).hexdigest()[:16]
            value["duplicate_of"] = representative if index else ""
            value["duplicate_count"] = len(values) - 1
            (alternatives if index else representatives).append(value)
    return representatives + alternatives


def search_package_candidates(
    *, slot: dict, brief: dict, media: list[dict], top_k: int | None = 8,
    excluded_visuals: tuple[str, ...] = (), preferred_identity: str = "",
    _prepared: tuple[list[dict], list[dict], dict[str, str], dict[str, dict]] | None = None,
    semantic_policy=None, semantic_cache_root=None,
) -> dict:
    manifest_rows, analysis_rows, cluster_ids, by_id = (
        _prepared if _prepared is not None else _package_search_inputs(media)
    )
    result = search_images_for_slot(
        slot, brief, manifest_rows, analysis_rows,
        top_k=max(1, len(manifest_rows)), excluded_visuals=excluded_visuals,
        semantic_assessment=assess_image_candidates(slot, brief, manifest_rows, analysis_rows,
            semantic_policy, semantic_cache_root) if semantic_policy is not None else None,
    )
    safe = {str(item.get("image_id") or "") for item in result.get("candidates") or []}
    if preferred_identity and preferred_identity in safe and not slot.get("current_visual_authority"):
        result["candidates"].sort(
            key=lambda item: (str(item.get("image_id") or "") != preferred_identity, -int(item.get("score") or 0))
        )
    for candidate in result.get("candidates") or []:
        source_item = by_id.get(str(candidate.get("image_id") or ""), {})
        candidate["content_sha256"] = str(source_item.get("content_sha256") or "")
        candidate["perceptual_cluster_id"] = cluster_ids.get(
            str(candidate.get("image_id") or ""), "",
        )
    result["candidates"] = _present_distinct_candidates(
        list(result.get("candidates") or []), by_id, None if top_k is None else max(1, int(top_k)),
    )
    return result


def _package_search_inputs(
    media: list[dict],
    *,
    include_perceptual_clusters: bool = True,
) -> tuple[list[dict], list[dict], dict[str, str], dict[str, dict]]:
    cluster_ids = (
        perceptual_cluster_ids([
            {"image_id": str(item["media_id"]), "path": item["path"]}
            for item in media
        ])
        if include_perceptual_clusters else {}
    )
    manifest_rows = []
    analysis_rows = []
    for item in media:
        source = dict(item.get("source") or {})
        image_id = str(item["media_id"])
        source_links = [dict(value) for value in item.get("source_links") or []]
        if not source_links:
            source_links = [{
                "source_file": str(source.get("file") or ""),
                "source_sheet": str(source.get("sheet") or ""),
                "source_row": int(source.get("row") or 1),
                "structure": "",
                "shot_no": "",
                "shot_function": str(item.get("purpose") or item.get("scene") or ""),
                "visual_subject": " ".join(item.get("subjects") or []),
                "tags": " ".join(item.get("tags") or []),
                "source_brand": str(item.get("brand") or ""),
            }]
        manifest_rows.append({
            "image_id": image_id,
            "content_sha256": str(item.get("content_sha256") or ""),
            "原图路径": str(item["path"]),
            "预览路径": str(item["path"]),
            "asset_role": "source_reference",
            "来源文件": str(source.get("file") or ""),
            "来源 sheet": str(source.get("sheet") or ""),
            "来源行": int(source.get("row") or 1),
            "source_semantic_text": str(item.get("source_semantic_text") or ""),
            "source_links": source_links,
            "package_sources": item.get('package_sources', []),
        })
        visual = normalize_visual_analysis(dict(item.get("visual_analysis") or {}))
        if visual:
            analysis_rows.append({
                **visual,
                "image_id": image_id,
                "caption": str(visual.get("caption") or ""),
                "tags": list(visual.get("tags") or []),
                "brand_names": list(visual.get("brand_names") or []),
                "negative_flags": normalize_visual_flags(visual.get("negative_flags")),
                "quality_flags": normalize_visual_flags(visual.get("quality_flags")),
                "ocr_text": str(visual.get("ocr_text") or ""),
                "people_count": visual.get("people_count"),
                "source": str(visual.get("source") or "asset_package_visual_analysis"),
            })
    by_id = {str(item["media_id"]): item for item in media}
    return manifest_rows, analysis_rows, cluster_ids, by_id


def search_package_library_candidates(*, brief: dict, media: list[dict], top_k: int | None = 12, browse: bool = False) -> dict:
    """Browse category media before a shot plan exists, still through canonical safety checks."""
    # No shot is assigned here. Pixel-cluster reuse checks run later during per-shot matching.
    prepared = _package_search_inputs(media, include_perceptual_clusters=False)
    if browse:
        return search_package_candidates(
            slot={"library_browse": True}, brief=brief, media=media,
            top_k=top_k, _prepared=prepared,
        )
    accepted: dict[str, dict] = {}
    rejected: dict[tuple[str, str], dict] = {}
    contexts: dict[str, tuple[dict, set[str]]] = {}
    for item in media:
        for link in item.get("source_links") or []:
            shot_no = str(link.get("shot_no") or "").strip()
            try:
                slot_index = int(shot_no)
            except (TypeError, ValueError):
                slot_index = len(contexts) + 1
            slot = {
                "slot_index": slot_index,
                "镜号": shot_no or str(slot_index),
                "结构": str(link.get("structure") or ""),
                "画面描述": str(link.get("visual_desc") or ""),
                "镜头功能": str(link.get("shot_function") or ""),
                "主视觉功能": str(link.get("shot_function") or ""),
                "采用来源文件": str(link.get("source_file") or ""),
                "采用来源 sheet": str(link.get("source_sheet") or ""),
                "采用来源行": int(link.get("source_row") or 1),
            }
            key = json.dumps(slot, ensure_ascii=False, sort_keys=True)
            if key not in contexts:
                contexts[key] = (slot, set())
            contexts[key][1].add(str(item["media_id"]))
    for slot, linked_media_ids in contexts.values():
        manifest_rows, analysis_rows, cluster_ids, by_id = prepared
        linked_prepared = (
            [
                item for item in manifest_rows
                if str(item.get("image_id") or "") in linked_media_ids
            ],
            [
                item for item in analysis_rows
                if str(item.get("image_id") or "") in linked_media_ids
            ],
            {
                image_id: cluster_id for image_id, cluster_id in cluster_ids.items()
                if image_id in linked_media_ids
            },
            {
                image_id: item for image_id, item in by_id.items()
                if image_id in linked_media_ids
            },
        )
        result = search_package_candidates(
            slot=slot, brief=brief, media=media, top_k=top_k,
            _prepared=linked_prepared,
        )
        for item in result.get("rejected") or []:
            rejected[(str(item.get("image_id") or ""), str(item.get("reason") or ""))] = item
        for item in result.get("candidates") or []:
            image_id = str(item.get("image_id") or "")
            current = accepted.get(image_id)
            if current is None or int(item.get("score") or 0) > int(current.get("score") or 0):
                accepted[image_id] = item
    candidates = _present_distinct_candidates(
        sorted(accepted.values(), key=lambda item: (-int(item.get("score") or 0), str(item.get("image_id") or ""))),
        {str(item["media_id"]): item for item in media}, top_k,
    )
    return {
        "status": "review" if candidates else "gap",
        "gap_reason": "" if candidates else "类别库中没有通过权威安全规则的候选",
        "candidates": candidates, "rejected": list(rejected.values()),
        "context_count": len(contexts),
    }


def search_project_candidates(
    *, slot: dict, brief: dict, assets, top_k: int | None = 8, excluded_visuals=(),
    preferred_visuals=(), forced_visual: str = "",
    semantic_policy=None, semantic_cache_root=None,
) -> dict:
    manifest_rows, analysis_rows = [], []
    cluster_ids = perceptual_cluster_ids([
        {
            "image_id": asset.image_id,
            "path": asset.controlled_path,
            "perceptual_hash": asset.business_summary.perceptual_hash,
        }
        for asset in assets
    ])
    for asset in assets:
        source = min(asset.source_links, key=lambda item: (item.source_label, item.sheet or "", item.row or 0))
        summary = asset.business_summary
        source_reference = any(
            str(item.source_document_id or "").startswith("package:")
            and not str(item.source_document_id or "").startswith(
                "package:formal-local-upload@"
            )
            for item in asset.source_links
        )
        source_links = [{
            "source_file": item.source_label,
            "source_sheet": item.sheet or "图片",
            "source_row": item.row or item.image or 1,
            "structure": summary.purpose,
            "shot_function": summary.purpose,
            "visual_subject": " ".join(summary.subjects),
            "tags": " ".join(summary.tags),
            "source_brand": summary.brand,
        } for item in asset.source_links]
        manifest_rows.append({
            "image_id": asset.image_id, "content_sha256": asset.image_sha256,
            "原图路径": str(asset.controlled_path),
            "预览路径": str(asset.controlled_path),
            "asset_role": "source_reference" if source_reference else "project_media",
            "来源文件": source.source_label, "来源 sheet": source.sheet or "图片", "来源行": source.row or source.image or 1,
            "source_semantic_text": getattr(summary, "source_semantic_text", "") or " ".join((summary.caption, summary.scene, summary.purpose, *summary.subjects, *summary.tags)),
            "source_links": source_links,
            "是否低清": summary.pixel_width < 1280 or summary.pixel_height < 720,
        })
        analysis_rows.append({
            **getattr(summary, "visual_observations", {}),
            "image_id": asset.image_id,
            "caption": "" if getattr(summary, "visual_analysis_available", None) is False else summary.caption,
            "content_sha256": getattr(summary, 'evidence_content_sha256', ''),
            "prompt_version": getattr(summary, 'evidence_prompt_version', ''),
            "scene": summary.scene,
            "tags": list(dict.fromkeys((*summary.tags, *summary.subjects))),
            "brand_names": ([summary.brand] if summary.brand else []),
            "quality_flags": list(dict.fromkeys([*summary.quality_flags, *getattr(summary, "visual_observations", {}).get("quality_flags", [])])),
            "negative_flags": list(dict.fromkeys([*summary.negative_flags, *getattr(summary, "visual_observations", {}).get("negative_flags", [])])),
            "ocr_text": str(getattr(summary, "visual_observations", {}).get("ocr_text") or ""), "source": "project_image_analysis",
        })
    result = search_images_for_slot(
        slot, brief, manifest_rows, analysis_rows,
        top_k=max(1, len(manifest_rows)), excluded_visuals=excluded_visuals,
        preferred_visuals=preferred_visuals, forced_visual=forced_visual,
        semantic_assessment=assess_image_candidates(slot, brief, manifest_rows, analysis_rows,
            semantic_policy, semantic_cache_root) if semantic_policy is not None else None,
    )
    by_id = {asset.image_id: asset for asset in assets}
    for candidate in result.get("candidates") or []:
        asset = by_id.get(str(candidate.get("image_id") or ""))
        if asset is not None:
            candidate["content_sha256"] = asset.image_sha256
            candidate["perceptual_cluster_id"] = cluster_ids.get(asset.image_id, "")
            candidate["quality_evidence"] = {
                "summary": asset.business_summary.quality,
                "quality_flags": list(asset.business_summary.quality_flags),
                "negative_flags": list(asset.business_summary.negative_flags),
                "pixel_width": asset.business_summary.pixel_width,
                "pixel_height": asset.business_summary.pixel_height,
            }
    result["candidates"] = _present_distinct_candidates(
        list(result.get("candidates") or []),
        {asset.image_id: {"path": asset.controlled_path} for asset in assets},
        None if top_k is None else max(1, int(top_k)),
    )
    return result


def candidate_snapshot_digest(*, project_id: str, version_id: str, shot_no: str, query: str, result: dict) -> str:
    payload = {
        "project_id": project_id,
        "version_id": version_id,
        "shot_no": shot_no,
        "query": query,
        "candidates": [
            {
                "image_id": item.get("image_id"),
                "content_sha256": item.get("content_sha256"),
                "score": item.get("score"),
                "match_reason": item.get("match_reason"),
                "conflicts": item.get("conflicts") or [],
                "perceptual_cluster_id": item.get("perceptual_cluster_id") or "",
                "quality_score": item.get("quality_score"),
                "quality_evidence": item.get("quality_evidence") or {},
            }
            for item in result.get("candidates") or []
        ],
        "rejected": [
            {"image_id": item.get("image_id"), "reason": item.get("reason")}
            for item in result.get("rejected") or []
        ],
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def resolve_legacy_visual_identity(binding: dict, media: list[dict]) -> dict:
    """Resolve only stable ID, content SHA, or exact SHA-verifiable manifest path."""
    def resolved(kind: str, media_id: str) -> dict:
        return {
            "status": "resolved",
            "resolution_kind": kind,
            "media_id": media_id,
            "candidate_badge": "来自旧版本稳定映射",
            "match_basis": kind,
        }

    by_id = {str(item["media_id"]): item for item in media}
    by_sha: dict[str, list[dict]] = {}
    by_path: dict[str, list[dict]] = {}
    for item in media:
        by_sha.setdefault(str(item["content_sha256"]), []).append(item)
        by_path.setdefault(str(Path(item["path"]).resolve()), []).append(item)
    for key in ("media_id", "image_id"):
        value = str(binding.get(key) or "")
        if value in by_id:
            return resolved("media_id", value)
    digest = str(binding.get("content_sha256") or binding.get("image_sha256") or "")
    exact_path = str(binding.get("original_path") or binding.get("原图路径") or "")
    if exact_path and Path(exact_path).is_absolute():
        matches = by_path.get(str(Path(exact_path).resolve()), [])
        if len(matches) == 1 and digest and matches[0]["content_sha256"] == digest:
            return resolved("manifest_exact", matches[0]["media_id"])
    if digest and len(by_sha.get(digest, [])) == 1:
        return resolved("content_sha256", by_sha[digest][0]["media_id"])
    if digest or exact_path or any(binding.get(key) for key in ("media_id", "image_id")):
        return {"status": "ambiguous", "resolution_kind": "ambiguous", "media_id": ""}
    return {"status": "unmapped", "resolution_kind": "ambiguous", "media_id": ""}
