from __future__ import annotations

import json
from pathlib import Path

from .image_assets import read_jsonl
from .image_retrieval import search_images_for_slot


class VisualCandidateError(ValueError):
    pass


def find_run_dir(cache_root: str | Path, run_id: str) -> Path:
    normalized = str(run_id).strip()
    if not normalized or Path(normalized).name != normalized:
        raise VisualCandidateError("run_id 无效")
    matches = [
        path.parent
        for path in Path(cache_root).glob(f"*/runs/{normalized}/run_manifest.json")
        if path.is_file()
    ]
    if len(matches) != 1:
        raise VisualCandidateError("找不到唯一的运行版本")
    return matches[0]


def candidates_for_run(
    run_dir: str | Path,
    *,
    shot_no: str,
    query: str = "",
    top_k: int = 6,
) -> dict:
    root = Path(run_dir)
    shot = str(shot_no).strip()
    if not shot.isdigit():
        raise VisualCandidateError("图片候选必须填写单个数字镜号")
    draft_path = root / "script_draft.json"
    plan_path = root / "script_plan.json"
    source_path = draft_path if draft_path.is_file() else plan_path
    manifest_path = root / "image_manifest.jsonl"
    analysis_path = root / "image_analysis.minimax.jsonl"
    if not source_path.is_file() or not manifest_path.is_file() or not analysis_path.is_file():
        raise VisualCandidateError("该版本缺少镜头计划或视觉索引，不能检索候选")
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    script_rows = list(payload.get("script_rows") or [])
    detail_rows = list(payload.get("detail_rows") or [])
    index = int(shot) - 1
    if index < 0 or index >= len(script_rows):
        raise VisualCandidateError(f"镜号超出范围: {shot}")
    slot = dict(detail_rows[index] if index < len(detail_rows) else {})
    slot.update(script_rows[index])
    normalized_query = str(query).strip()
    if normalized_query:
        slot["画面描述"] = "；".join(
            value for value in (str(slot.get("画面描述") or "").strip(), normalized_query) if value
        )
    manifest_rows = read_jsonl(manifest_path)
    analysis_rows = read_jsonl(analysis_path)
    analyzed_ids = {str(item.get("image_id") or "") for item in analysis_rows}
    failed_analysis_ids = {
        str(item.get("image_id") or "")
        for item in analysis_rows
        if str(item.get("vision_recognition_status") or "") == "failed"
    }
    result = search_images_for_slot(
        slot,
        dict(payload.get("brief") or {}),
        manifest_rows,
        analysis_rows,
        top_k=max(1, min(int(top_k), 12)),
    )
    binding_path = root / "visual_binding.json"
    current = {}
    binding_state = {}
    if binding_path.is_file():
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        binding_state = next(
            (
                item for item in binding.get("bindings") or []
                if str(item.get("slot_index") or item.get("shot_no") or "") == shot
            ),
            {},
        )
        for item in binding.get("visual_asset_rows") or []:
            if str(item.get("slot_index") or item.get("镜号") or "") == shot:
                current = {
                    "image_id": str(item.get("image_id") or ""),
                    "path": str(item.get("原图路径") or ""),
                    "summary": str(item.get("视觉内容摘要") or ""),
                }
                break
    has_current = bool(
        str(current.get("image_id") or "").strip()
        and str(current.get("path") or "").strip()
    )
    return {
        "run_id": root.name,
        "script_type": str((payload.get("brief") or {}).get("脚本类型") or ""),
        "shot_no": shot,
        "query": normalized_query,
        "current": current,
        "shot_context": {
            "structure": str(slot.get("结构") or slot.get("模板结构") or ""),
            "visual_description": str(slot.get("画面描述") or ""),
            "slot_function": str(slot.get("槽位功能") or ""),
            "required_visual_evidence": str(slot.get("必须视觉证据") or ""),
            "source_file": str(slot.get("采用来源文件") or slot.get("模板来源文件") or ""),
            "source_sheet": str(slot.get("采用来源 sheet") or slot.get("模板来源 sheet") or ""),
            "source_row": str(slot.get("采用来源行") or slot.get("模板来源行") or ""),
            "reference_paths": str(slot.get("画面示意") or ""),
        },
        "pool": {
            "indexed": len(manifest_rows),
            "visually_analyzed": len(analyzed_ids),
            "visual_succeeded": len(analyzed_ids - failed_analysis_ids),
            "visual_failed": len(failed_analysis_ids),
            "eligible": int(result.get("eligible_count") or 0),
            "visual_verified": int(result.get("visual_verified_count") or 0),
            "source_review": int(result.get("source_review_count") or 0),
            "brand_review": int(result.get("brand_review_count") or 0),
            "shown": len(result.get("candidates") or []),
            "rejected": len(result.get("rejected") or []),
        },
        "resolution_status": str(
            binding_state.get("resolution_status")
            or ("resolved" if has_current else "visual_resolution_required")
        ),
        "document_basis_available": bool(binding_state.get("document_basis_available")),
        "recommended_action": str(binding_state.get("recommended_action") or ""),
        "gap_reason": result.get("gap_reason") or "",
        "candidates": list(result.get("candidates") or []),
        "rejected_count": len(result.get("rejected") or []),
    }
