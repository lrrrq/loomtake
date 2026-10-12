from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from .ai_spec import AiSpecError, load_ai_spec_shots
from .asset_storage import LocalAssetStorage
from .excel_io import (
    IMAGE_EXTENSIONS,
    load_creative_variants,
    load_template_slots,
)
from .models import SourceShot
from .script_type_adapter import (
    category_id_for_script_type,
    metadata_for_category_id,
    resolve_script_type_workbook,
)
from .run_contract import sha256_file, sha256_json
from .run_state import write_json_atomic
from .template_rules import (
    TEMPLATE_RULES_NAME,
    template_rule_contract,
    write_builtin_template_rules,
)


PACKAGE_SCHEMA_VERSION = "mplus-asset-package/v1"
PACKAGE_V2_SCHEMA_VERSION = "mplus-asset-package/v2"
PACKAGE_V3_SCHEMA_VERSION = "mplus-asset-package/v3"
SUPPORTED_PACKAGE_SCHEMA_VERSIONS = {
    PACKAGE_SCHEMA_VERSION,
    PACKAGE_V2_SCHEMA_VERSION,
    PACKAGE_V3_SCHEMA_VERSION,
}
INDEX_SCHEMA_VERSION = "mplus-asset-index/v4"
MANIFEST_NAME = "asset_package.json"
INDEX_NAME = "asset_index.sqlite3"


def _open_readonly_index(path: str | Path) -> sqlite3.Connection:
    uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


class AssetPackageError(ValueError):
    pass


def media_source_provenance(
    connection: sqlite3.Connection, media_id: str, *, source_workbook_sha256: str
) -> list[dict]:
    """Read all explicit extraction anchors without guessing from a filename.

    Called against the source index before bundling collapses media locations.
    Old locations without extraction evidence remain valid legacy media.
    """
    records = []
    for (location,) in connection.execute(
        "SELECT path FROM media_locations WHERE media_id=? ORDER BY path", (media_id,)
    ):
        path = Path(str(location))
        evidence = path.with_suffix(f".{source_workbook_sha256}.provenance.json")
        if not evidence.is_file():
            continue
        record = json.loads(evidence.read_text(encoding="utf-8"))
        if (record.get("source_workbook_sha256") != source_workbook_sha256
                or record.get("normalized_png_sha256") != sha256_file(path)):
            raise AssetPackageError("图片来源锚点证据与当前内容不匹配")
        records.append(record)
    return records


def script_payload_digest_manifest(connection: sqlite3.Connection) -> dict:
    """Build optional candidate metadata after package-relative paths are final.

    source_sha256 retains its historical pre-bundle meaning. Payload hashes use
    run_contract.sha256_json (UTF-8, sorted keys, no ASCII escaping, compact JSON)
    and never include this separate digest metadata or runtime-resolved paths.
    """
    records = {}
    for asset_id, source_digest, raw_payload in connection.execute(
        "SELECT asset_id, source_sha256, payload_json FROM script_assets ORDER BY asset_id"
    ):
        records[str(asset_id)] = {
            "source_record_sha256": str(source_digest),
            "packaged_payload_sha256": sha256_json(json.loads(str(raw_payload))),
        }
    return {
        "schema_version": "mplus-script-payload-digests/v1",
        "canonicalization": "sha256_json",
        "records": records,
    }


def verify_script_payload_digest(
    metadata: dict | None, asset_id: str, payload: dict, *, source_sha256: str
) -> bool:
    """Return False for legacy absence; verify declared candidate evidence.

    Call before converting relative media paths into runtime absolute paths.
    This does not claim a legacy source digest verifies the packaged payload.
    """
    if metadata is None:
        return False
    if (not isinstance(metadata, dict) or metadata.get("schema_version") != "mplus-script-payload-digests/v1"
            or metadata.get("canonicalization") != "sha256_json"):
        raise AssetPackageError("脚本封装摘要版本不受支持")
    record = metadata.get("records", {}).get(str(asset_id))
    if not isinstance(record, dict):
        raise AssetPackageError(f"脚本封装摘要缺失: {asset_id}")
    if record.get("source_record_sha256") != source_sha256:
        raise AssetPackageError(f"脚本来源摘要不匹配: {asset_id}")
    if record.get("packaged_payload_sha256") != sha256_json(payload):
        raise AssetPackageError(f"脚本封装内容摘要不匹配: {asset_id}")
    return True


def create_asset_package(
    package_dir: str | Path,
    *,
    package_id: str,
    source_root: str | Path,
    media_roots: list[str | Path] | None = None,
    metadata_roots: list[str | Path] | None = None,
    category_id: str = "launch",
    source_workbook: str | Path | None = None,
) -> Path:
    metadata_for_category_id(category_id)
    root = Path(package_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = Path(source_root).expanduser().resolve()
    if not source.is_dir():
        raise AssetPackageError(f"资料库目录不存在: {source}")
    manifest = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_id": str(package_id).strip(),
        "category_id": str(category_id).strip(),
        "source_root": _portable_path(root, source),
        "media_roots": [_portable_path(root, Path(item).expanduser().resolve()) for item in (media_roots or [])],
        "metadata_roots": [_portable_path(root, Path(item).expanduser().resolve()) for item in (metadata_roots or [])],
        "index": INDEX_NAME,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    if not manifest["package_id"]:
        raise AssetPackageError("package_id 不得为空")
    if source_workbook is not None:
        workbook = Path(source_workbook).expanduser().resolve()
        if not workbook.is_file() or not workbook.is_relative_to(source):
            raise AssetPackageError("指定模板必须位于资料库目录内")
        manifest["source_workbook"] = str(workbook.relative_to(source))
    path = root / MANIFEST_NAME
    write_json_atomic(path, manifest)
    update_asset_package_index(path)
    return path


def load_asset_package(path: str | Path) -> dict:
    requested = Path(path).expanduser().resolve()
    manifest_path = requested / MANIFEST_NAME if requested.is_dir() else requested
    if not manifest_path.is_file():
        raise AssetPackageError(f"资产包清单不存在: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") not in SUPPORTED_PACKAGE_SCHEMA_VERSIONS:
        raise AssetPackageError("资产包版本不受支持")
    root = manifest_path.parent
    source_root = _resolve_package_path(root, payload.get("source_root"))
    media_roots = [_resolve_package_path(root, item) for item in payload.get("media_roots") or []]
    metadata_roots = [_resolve_package_path(root, item) for item in payload.get("metadata_roots") or []]
    return {
        **payload,
        "manifest_path": manifest_path,
        "package_root": root,
        "source_root_path": source_root,
        "source_workbook_path": _source_workbook_path(payload, source_root),
        "media_root_paths": media_roots,
        "metadata_root_paths": metadata_roots,
        "index_path": _resolve_package_path(root, payload.get("index") or INDEX_NAME),
        "template_rule_contract": template_rule_contract({
            **payload,
            "package_root": root,
        }),
        "source_semantics": _source_semantics_contract(payload, root),
    }


def _source_semantics_contract(payload: dict, root: Path) -> dict:
    relative = str(payload.get("source_semantics_file") or "")
    if not relative:
        return {"status": "not_recorded", "label": "未记录", "sheets": []}
    path = (root / relative).resolve()
    if (not path.is_relative_to(root) or not path.is_file()
            or sha256_file(path) != payload.get("source_semantics_sha256")):
        raise AssetPackageError("原知识语义文件缺失或 SHA 已漂移")
    context = json.loads(path.read_text(encoding="utf-8"))
    if (context.get("schema_version") != "mplus-source-semantics/v1"
            or context.get("workbook_sha256") != payload.get("source_workbook_sha256")):
        raise AssetPackageError("原知识语义与来源工作簿不匹配")
    return {**context, "status": "recorded", "sha256": payload["source_semantics_sha256"]}


def _source_workbook_path(package: dict, source_root: Path) -> Path:
    relative = str(package.get("source_workbook") or "").strip()
    if relative:
        workbook = (source_root / relative).resolve()
        if not workbook.is_relative_to(source_root.resolve()):
            raise AssetPackageError("模板来源超出资料库目录")
        return workbook
    metadata = metadata_for_category_id(package["category_id"])
    return resolve_script_type_workbook(source_root, metadata.display_name)


def asset_package_index_summary(path: str | Path) -> dict:
    package = load_asset_package(path)
    summary = {
        "package_id": str(package.get("package_id") or ""),
        "category_id": str(package.get("category_id") or ""),
        **dict(package.get("index_counts") or {}),
    }
    index_path = Path(package["index_path"])
    if not index_path.is_file():
        return summary
    connection = _open_readonly_index(index_path)
    try:
        package_id = package["package_id"]
        summary.update({
            "script_assets": connection.execute(
                "SELECT COUNT(*) FROM script_assets WHERE package_id=?", (package_id,)
            ).fetchone()[0],
            "creative_script_assets": connection.execute(
                "SELECT COUNT(*) FROM script_assets WHERE package_id=? AND source_sheet='创意库'",
                (package_id,),
            ).fetchone()[0],
            "media_assets": connection.execute(
                "SELECT COUNT(*) FROM media_assets WHERE package_id=?", (package_id,)
            ).fetchone()[0],
            "media_with_analysis": connection.execute(
                "SELECT COUNT(*) FROM media_assets WHERE package_id=? AND analysis_json IS NOT NULL",
                (package_id,),
            ).fetchone()[0],
            "media_with_successful_analysis": connection.execute(
                "SELECT COUNT(*) FROM media_assets WHERE package_id=? AND analysis_json IS NOT NULL "
                "AND COALESCE(json_extract(analysis_json, '$.vision_recognition_status'), '') <> 'failed'",
                (package_id,),
            ).fetchone()[0],
            "media_with_failed_analysis": connection.execute(
                "SELECT COUNT(*) FROM media_assets WHERE package_id=? AND analysis_json IS NOT NULL "
                "AND json_extract(analysis_json, '$.vision_recognition_status') = 'failed'",
                (package_id,),
            ).fetchone()[0],
            "media_pending_analysis": connection.execute(
                "SELECT COUNT(*) FROM media_assets WHERE package_id=? AND analysis_json IS NULL",
                (package_id,),
            ).fetchone()[0],
            "media_with_source_context": connection.execute(
                "SELECT COUNT(DISTINCT media_id) FROM media_script_links WHERE package_id=?",
                (package_id,),
            ).fetchone()[0],
            "creative_media_with_source_context": connection.execute(
                "SELECT COUNT(DISTINCT media_id) FROM media_script_links "
                "WHERE package_id=? AND source_sheet='创意库'",
                (package_id,),
            ).fetchone()[0],
            "media_source_links": connection.execute(
                "SELECT COUNT(*) FROM media_script_links WHERE package_id=?", (package_id,)
            ).fetchone()[0],
        })
    finally:
        connection.close()
    return summary


def asset_package_source_record(
    path: str | Path,
    *,
    expected_category_id: str | None = None,
) -> dict:
    package = load_asset_package(path)
    if expected_category_id and package.get("category_id") != expected_category_id:
        raise AssetPackageError(
            f"资产包类别 {package.get('category_id')} 与当前运行类别 "
            f"{expected_category_id} 不一致"
        )
    if package.get("index_schema_version") != INDEX_SCHEMA_VERSION:
        raise AssetPackageError("资产包索引版本已过期，请先更新资产包")
    metadata = metadata_for_category_id(package.get("category_id"))
    workbook_path = package["source_workbook_path"]
    if not workbook_path.is_file():
        raise AssetPackageError(f"资产包来源工作簿不存在: {workbook_path}")
    stat = workbook_path.stat()
    if (
        package.get("source_workbook_size") != stat.st_size
        or package.get("source_workbook_mtime_ns") != stat.st_mtime_ns
        or not package.get("source_workbook_sha256")
    ):
        raise AssetPackageError("来源工作簿已变化，请先更新资产包索引")
    if not package["index_path"].is_file():
        raise AssetPackageError(f"资产包索引不存在: {package['index_path']}")
    return {
        "package_id": package["package_id"],
        "category_id": package["category_id"],
        "source_view": str(package.get("index_source_view") or "human"),
        "source_workbook_path": str(workbook_path.resolve()),
        "source_workbook_sha256": str(package["source_workbook_sha256"]),
        "source_workbook_size": stat.st_size,
        "source_workbook_mtime_ns": stat.st_mtime_ns,
        "index_path": str(package["index_path"].resolve()),
        "index_fingerprint": sha256_json({
            "package_id": package["package_id"],
            "index_schema_version": package["index_schema_version"],
            "source_workbook_sha256": package["source_workbook_sha256"],
            "source_view": str(package.get("index_source_view") or "human"),
        }),
        "template_rule_contract": package["template_rule_contract"],
        "source_semantics": package["source_semantics"],
    }


def source_in_frozen_library(source: SourceShot | dict, package_evidence: dict | None) -> bool:
    """Authorize creative reuse against the run's verified immutable bindings."""
    row = asdict(source) if isinstance(source, SourceShot) else source
    if not all(row.get(key) for key in ('source_package_id', 'source_package_version',
            'source_category_id', 'source_asset_id', 'source_file', 'source_sheet', 'source_row')):
        return False
    return any(
        binding.get('package_id') == row['source_package_id']
        and binding.get('package_version') == row['source_package_version']
        and binding.get('category_id') == row['source_category_id']
        and binding.get('resolved_ref') == f"{row['source_package_id']}@{row['source_package_version']}"
        and isinstance(binding.get('package_sha256'), str) and len(binding['package_sha256']) == 64
        for binding in (package_evidence or {}).get('library_bindings', [])
    )


def package_script_catalog(
    path: str | Path,
    *,
    expected_category_id: str,
    source_view: str = "ai-spec",
    library_paths: tuple[str | Path, ...] | list[str | Path] = (),
) -> tuple[list[SourceShot], list[SourceShot], dict]:
    package = load_asset_package(path)
    source_record = asset_package_source_record(
        path,
        expected_category_id=expected_category_id,
    )
    if source_record["source_view"] != source_view:
        raise AssetPackageError(
            f"资产包文本索引为 {source_record['source_view']}，当前要求 {source_view}，"
            "请先用已就绪 AI_规范表更新资产包"
        )
    connection = _open_readonly_index(package["index_path"])
    try:
        rows = connection.execute(
            "SELECT asset_id, source_sha256, payload_json FROM script_assets WHERE package_id=? "
            "AND category_id=? ORDER BY source_sheet, source_row",
            (package["package_id"], expected_category_id),
        ).fetchall()
    finally:
        connection.close()
    template = []
    creative = []
    for asset_id, source_digest, raw_payload in rows:
        payload = json.loads(str(raw_payload))
        verify_script_payload_digest(package.get("script_payload_digests"), str(asset_id), payload,
                                     source_sha256=str(source_digest))
        record_type = str(payload.pop("record_type", ""))
        if package.get("schema_version") in {PACKAGE_V2_SCHEMA_VERSION, PACKAGE_V3_SCHEMA_VERSION}:
            for field in ("reference_image_path", "effect_reference_path"):
                payload[field] = _runtime_package_reference_paths(
                    package,
                    payload.get(field),
                )
        shot = replace(SourceShot(**payload), source_package_id=package["package_id"],
            source_package_version=int(package.get("package_version") or 0),
            source_category_id=package["category_id"], source_asset_id=str(asset_id))
        if record_type == "creative" or shot.source_sheet == "创意库":
            creative.append(shot)
        else:
            template.append(shot)
    if not template:
        raise AssetPackageError("资产包文本索引未包含可用模版记录")
    if library_paths:
        def identity(shot):
            return sha256_json({key: value for key, value in asdict(shot).items()
                                if key not in {"reference_image_path", "effect_reference_path"}})
        seen = {identity(shot) for shot in creative}
        sources = []
        for library_path in library_paths:
            library_category = load_asset_package(library_path)["category_id"]
            _, additional, source = package_script_catalog(library_path,
                expected_category_id=library_category, source_view=source_view)
            sources.append(source)
            for shot in additional:
                key = identity(shot)
                if key not in seen:
                    seen.add(key)
                    creative.append(shot)
        source_record = {**source_record, "library_sources": sources}
    return template, creative, source_record


def update_asset_package_index(path: str | Path) -> dict:
    package = load_asset_package(path)
    if package.get("schema_version") in {PACKAGE_V2_SCHEMA_VERSION, PACKAGE_V3_SCHEMA_VERSION}:
        raise AssetPackageError("自包含资产包 v2/v3 为不可变版本，不允许原地更新")
    source_root = package["source_root_path"]
    if not source_root.is_dir():
        raise AssetPackageError(f"资料库目录不存在: {source_root}")
    manifest_payload = json.loads(package["manifest_path"].read_text(encoding="utf-8"))
    metadata = metadata_for_category_id(package.get("category_id"))
    workbook_path = package["source_workbook_path"]
    if not workbook_path.is_file():
        raise AssetPackageError(
            f"资产包 {package['package_id']} 缺少{metadata.display_name}工作簿: {workbook_path}"
        )
    workbook_stat = workbook_path.stat()
    workbook_stat_unchanged = (
        manifest_payload.get("source_workbook_size") == workbook_stat.st_size
        and manifest_payload.get("source_workbook_mtime_ns") == workbook_stat.st_mtime_ns
        and bool(manifest_payload.get("source_workbook_sha256"))
    )
    workbook_sha256 = (
        str(manifest_payload["source_workbook_sha256"])
        if workbook_stat_unchanged
        else sha256_file(workbook_path)
    )
    connection = sqlite3.connect(package["index_path"])
    try:
        _create_schema(connection)
        existing_script_count = connection.execute("SELECT COUNT(*) FROM script_assets").fetchone()[0]
        source_changed = (
            manifest_payload.get("source_workbook_sha256") != workbook_sha256
            or manifest_payload.get("index_schema_version") != INDEX_SCHEMA_VERSION
            or existing_script_count == 0
        )
        source_view = str(manifest_payload.get("index_source_view") or "human")
        shot_records: list[tuple[str, SourceShot]] = []
        if source_changed:
            try:
                template, creative = load_ai_spec_shots(
                    source_root,
                    script_type=metadata.display_name,
                    workbook_path=workbook_path,
                )
                source_view = "ai-spec"
            except AiSpecError:
                template = load_template_slots(
                    source_root,
                    script_type=metadata.display_name,
                    workbook_path=workbook_path,
                )
                creative = load_creative_variants(
                    source_root,
                    script_type=metadata.display_name,
                    workbook_path=workbook_path,
                )
                source_view = "human"
            shot_records = [
                *(('template', shot) for shot in template),
                *(('creative', shot) for shot in creative),
            ]
            connection.execute(
                "DELETE FROM script_assets WHERE package_id=?",
                (package["package_id"],),
            )
        else:
            rows = connection.execute(
                "SELECT payload_json FROM script_assets WHERE package_id=? "
                "ORDER BY source_sheet, source_row",
                (package["package_id"],),
            ).fetchall()
            for (raw_payload,) in rows:
                payload = json.loads(str(raw_payload))
                record_type = str(payload.pop("record_type", ""))
                shot_records.append((record_type, SourceShot(**payload)))
        shots = [shot for _, shot in shot_records]
        for record_type, shot in shot_records:
            payload = {"record_type": record_type, **asdict(shot)}
            asset_id = _script_asset_id(package["package_id"], shot.source_sheet, shot.source_row)
            searchable = shot.searchable_text()
            connection.execute(
                """INSERT INTO script_assets(
                    asset_id, package_id, category_id, source_file, source_sheet, source_row,
                    structure, shot_no, visual_desc, searchable_text, source_sha256, payload_json, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(asset_id) DO UPDATE SET
                    source_file=excluded.source_file, structure=excluded.structure,
                    shot_no=excluded.shot_no, visual_desc=excluded.visual_desc,
                    searchable_text=excluded.searchable_text, source_sha256=excluded.source_sha256,
                    payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
                (
                    asset_id, package["package_id"], package["category_id"], shot.source_file,
                    shot.source_sheet, shot.source_row, shot.structure, shot.shot_no,
                    shot.visual_desc, searchable, sha256_json(payload),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True), _utc_now(),
                ),
            )
        media_paths = _collect_media_paths(shots, package["media_root_paths"])
        hashed_media_count = 0
        reused_media_count = 0
        for media_path in media_paths:
            stat = media_path.stat()
            existing = connection.execute(
                "SELECT media_id, size_bytes, mtime_ns FROM media_locations WHERE path=?",
                (str(media_path),),
            ).fetchone()
            if existing and int(existing[1] or 0) == stat.st_size and int(existing[2] or 0) == stat.st_mtime_ns:
                reused_media_count += 1
                continue
            digest = sha256_file(media_path)
            hashed_media_count += 1
            media_id = f"media_{digest[:20]}"
            connection.execute(
                """INSERT INTO media_assets(
                    media_id, package_id, content_sha256, current_path, filename, extension,
                    size_bytes, mtime_ns, searchable_text, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(media_id) DO UPDATE SET
                    current_path=excluded.current_path, filename=excluded.filename,
                    extension=excluded.extension, size_bytes=excluded.size_bytes,
                    mtime_ns=excluded.mtime_ns, searchable_text=excluded.searchable_text,
                    updated_at=excluded.updated_at""",
                (
                    media_id, package["package_id"], digest, str(media_path), media_path.name,
                    media_path.suffix.casefold(), media_path.stat().st_size,
                    media_path.stat().st_mtime_ns,
                    media_path.stem.replace("_", " ").replace("-", " "), _utc_now(),
                ),
            )
            connection.execute(
                """INSERT INTO media_locations(path, media_id, size_bytes, mtime_ns, last_seen_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                   media_id=excluded.media_id, size_bytes=excluded.size_bytes,
                   mtime_ns=excluded.mtime_ns, last_seen_at=excluded.last_seen_at""",
                (str(media_path), media_id, stat.st_size, stat.st_mtime_ns, _utc_now()),
            )
        connection.execute(
            "DELETE FROM media_script_links WHERE package_id=?",
            (package["package_id"],),
        )
        _index_source_media_links(connection, package, shot_records)
        metadata_stats = _apply_analysis_metadata(connection, package["metadata_root_paths"])
        _refresh_media_searchable_text(connection, package["package_id"])
        connection.commit()
        _rebuild_fts(connection)
        counts = {
            "script_assets": connection.execute("SELECT COUNT(*) FROM script_assets").fetchone()[0],
            "media_assets": connection.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0],
            "source_reindexed": source_changed,
            "media_hashed": hashed_media_count,
            "media_reused": reused_media_count,
            "media_with_analysis": connection.execute(
                "SELECT COUNT(*) FROM media_assets WHERE analysis_json IS NOT NULL"
            ).fetchone()[0],
            "media_with_successful_analysis": connection.execute(
                "SELECT COUNT(*) FROM media_assets WHERE analysis_json IS NOT NULL "
                "AND COALESCE(json_extract(analysis_json, '$.vision_recognition_status'), '') <> 'failed'"
            ).fetchone()[0],
            "media_with_failed_analysis": connection.execute(
                "SELECT COUNT(*) FROM media_assets WHERE analysis_json IS NOT NULL "
                "AND json_extract(analysis_json, '$.vision_recognition_status') = 'failed'"
            ).fetchone()[0],
            "media_pending_analysis": connection.execute(
                "SELECT COUNT(*) FROM media_assets WHERE analysis_json IS NULL"
            ).fetchone()[0],
            "media_with_source_context": connection.execute(
                "SELECT COUNT(DISTINCT media_id) FROM media_script_links WHERE package_id=?",
                (package["package_id"],),
            ).fetchone()[0],
            "media_source_links": connection.execute(
                "SELECT COUNT(*) FROM media_script_links WHERE package_id=?",
                (package["package_id"],),
            ).fetchone()[0],
            "metadata_files_read": metadata_stats["read"],
            "metadata_files_reused": metadata_stats["reused"],
            "source_workbook_hash_reused": workbook_stat_unchanged,
        }
    finally:
        connection.close()
    manifest = json.loads(package["manifest_path"].read_text(encoding="utf-8"))
    manifest.update({
        "updated_at": _utc_now(),
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "index_counts": counts,
        "source_workbook_sha256": workbook_sha256,
        "source_workbook_size": workbook_stat.st_size,
        "source_workbook_mtime_ns": workbook_stat.st_mtime_ns,
        "index_source_view": source_view,
    })
    write_json_atomic(package["manifest_path"], manifest)
    return {**counts, "index_path": str(package["index_path"]), "manifest_path": str(package["manifest_path"])}


def bundle_asset_package_v2(
    source_package: str | Path,
    target_dir: str | Path,
    *,
    package_version: int = 1,
) -> Path:
    source = load_asset_package(source_package)
    if source.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        raise AssetPackageError("只能从可更新的 v1 资产包构建 v2")
    if int(package_version) < 1:
        raise AssetPackageError("package_version 必须为正整数")
    target = Path(target_dir).expanduser().resolve()
    if target.exists():
        raise AssetPackageError(f"目标资产包版本已存在: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    building = target.parent / f".{target.name}.building-{uuid.uuid4().hex}"
    building.mkdir()
    try:
        source_record = asset_package_source_record(source["manifest_path"])
        source_workbook = Path(source_record["source_workbook_path"])
        sources_root = building / "sources"
        sources_root.mkdir()
        workbook_snapshot = sources_root / source_workbook.name
        shutil.copy2(source_workbook, workbook_snapshot)
        if sha256_file(workbook_snapshot) != source_record["source_workbook_sha256"]:
            raise AssetPackageError("来源工作簿快照校验失败")

        index_path = building / INDEX_NAME
        source_connection = _open_readonly_index(source["index_path"])
        target_connection = sqlite3.connect(index_path)
        try:
            source_connection.backup(target_connection)
            _create_schema(target_connection)
            target_connection.row_factory = sqlite3.Row
            media_rows = target_connection.execute(
                "SELECT media_id, content_sha256, current_path, filename FROM media_assets "
                "WHERE package_id=? ORDER BY media_id",
                (source["package_id"],),
            ).fetchall()
            storage = LocalAssetStorage(building / "blobs")
            blob_records = []
            target_connection.execute(
                "DELETE FROM media_locations WHERE media_id IN "
                "(SELECT media_id FROM media_assets WHERE package_id=?)",
                (source["package_id"],),
            )
            for row in media_rows:
                original = Path(_runtime_package_path(source, row["current_path"]))
                if not original.is_file():
                    raise AssetPackageError(f"资产包媒体不存在: {original}")
                with original.open("rb") as stream:
                    storage_ref, digest = storage.put(stream)
                if digest != str(row["content_sha256"]):
                    raise AssetPackageError(f"资产包媒体哈希不一致: {original}")
                object_path = storage.path_for(storage_ref)
                suffix = original.suffix.casefold()
                media_path = building / "media" / digest[:2] / f"{digest}{suffix}"
                media_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(object_path, media_path)
                except OSError:
                    shutil.copy2(object_path, media_path)
                relative_path = str(media_path.relative_to(building))
                stat = media_path.stat()
                target_connection.execute(
                    "UPDATE media_assets SET current_path=?, storage_ref=?, size_bytes=?, "
                    "mtime_ns=?, updated_at=? WHERE media_id=?",
                    (relative_path, storage_ref, stat.st_size, stat.st_mtime_ns, _utc_now(), row["media_id"]),
                )
                target_connection.execute(
                    "INSERT INTO media_locations(path, media_id, size_bytes, mtime_ns, last_seen_at) "
                    "VALUES(?,?,?,?,?)",
                    (relative_path, row["media_id"], stat.st_size, stat.st_mtime_ns, _utc_now()),
                )
                blob_records.append({
                    "media_id": str(row["media_id"]),
                    "filename": str(row["filename"] or ""),
                    "content_sha256": digest,
                    "storage_ref": storage_ref,
                    "path": relative_path,
                    "size_bytes": stat.st_size,
                    "source_anchors": media_source_provenance(source_connection, str(row["media_id"]),
                        source_workbook_sha256=source_record["source_workbook_sha256"]),
                })
            script_rows = target_connection.execute(
                "SELECT asset_id, payload_json FROM script_assets WHERE package_id=?",
                (source["package_id"],),
            ).fetchall()
            for script_row in script_rows:
                payload = json.loads(str(script_row["payload_json"]))
                for reference_kind, field in (
                    ("reference", "reference_image_path"),
                    ("effect", "effect_reference_path"),
                ):
                    paths = [
                        str(item[0])
                        for item in target_connection.execute(
                            "SELECT media.current_path FROM media_script_links AS links "
                            "JOIN media_assets AS media ON media.media_id=links.media_id "
                            "WHERE links.asset_id=? AND links.reference_kind=? "
                            "ORDER BY media.media_id",
                            (script_row["asset_id"], reference_kind),
                        ).fetchall()
                    ]
                    if paths:
                        payload[field] = ";".join(paths)
                target_connection.execute(
                    "UPDATE script_assets SET payload_json=?, updated_at=? WHERE asset_id=?",
                    (
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        _utc_now(),
                        script_row["asset_id"],
                    ),
                )
            payload_digests = script_payload_digest_manifest(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()

        blob_manifest_path = building / "blob_manifest.jsonl"
        _write_jsonl_atomic(blob_manifest_path, blob_records)
        workbook_stat = workbook_snapshot.stat()
        source_manifest = json.loads(source["manifest_path"].read_text(encoding="utf-8"))
        now = _utc_now()
        manifest = {
            "schema_version": PACKAGE_V2_SCHEMA_VERSION,
            "package_id": source["package_id"],
            "package_version": int(package_version),
            "category_id": source["category_id"],
            "status": "published",
            "source_root": "sources",
            "source_workbook": workbook_snapshot.name,
            "media_roots": ["media"],
            "metadata_roots": [],
            "index": INDEX_NAME,
            "blob_manifest": blob_manifest_path.name,
            "output_template": str(workbook_snapshot.relative_to(building)),
            "created_at": now,
            "updated_at": now,
            "published_at": now,
            "index_schema_version": source_manifest.get("index_schema_version"),
            "index_source_view": source_manifest.get("index_source_view"),
            "index_counts": source_manifest.get("index_counts") or {},
            "source_workbook_sha256": source_record["source_workbook_sha256"],
            "source_workbook_size": workbook_stat.st_size,
            "source_workbook_mtime_ns": workbook_stat.st_mtime_ns,
            "index_sha256": sha256_file(index_path),
            "script_payload_digests": payload_digests,
            "blob_count": len(blob_records),
            "blob_manifest_sha256": sha256_file(blob_manifest_path),
            "compatibility": {"reader": "mvp_v2", "legacy_v1_readable": True},
        }
        write_json_atomic(building / MANIFEST_NAME, manifest)
        os.replace(building, target)
        return target / MANIFEST_NAME
    except Exception:
        if building.exists():
            shutil.rmtree(building)
        raise


def bundle_asset_package_v3(
    source_package: str | Path,
    target_dir: str | Path,
    *,
    package_version: int = 1,
    template_rules: dict | None = None,
) -> Path:
    """Publish an immutable self-contained package with a bound rule manifest."""
    target = Path(target_dir).expanduser().resolve()
    if target.exists():
        raise AssetPackageError(f"目标资产包版本已存在: {target}")
    seed = target.parent / f".{target.name}.v3-seed-{uuid.uuid4().hex}"
    try:
        source = load_asset_package(source_package)
        if source.get("schema_version") == PACKAGE_SCHEMA_VERSION:
            manifest_path = bundle_asset_package_v2(
                source_package,
                seed,
                package_version=package_version,
            )
        elif source.get("schema_version") == PACKAGE_V2_SCHEMA_VERSION:
            shutil.copytree(source["package_root"], seed)
            manifest_path = seed / MANIFEST_NAME
            copied = json.loads(manifest_path.read_text(encoding="utf-8"))
            copied.update({
                "package_version": int(package_version),
                "updated_at": _utc_now(),
                "published_at": _utc_now(),
            })
            write_json_atomic(manifest_path, copied)
            asset_package_source_record(manifest_path)
        elif source.get("schema_version") == PACKAGE_V3_SCHEMA_VERSION:
            asset_package_source_record(source["manifest_path"])
            shutil.copytree(source["package_root"], seed)
            manifest_path = seed / MANIFEST_NAME
            copied = json.loads(manifest_path.read_text(encoding="utf-8"))
            now = _utc_now()
            copied.update({
                "package_version": int(package_version),
                "updated_at": now,
                "published_at": now,
            })
            write_json_atomic(manifest_path, copied)
            # The authoritative builder re-validates the copied package before
            # it becomes visible; callers still bind the final record SHA later.
            asset_package_source_record(manifest_path)
        else:
            raise AssetPackageError("只能从可更新的 v1 或已发布的 v2/v3 资产包构建 v3")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if template_rules is not None:
            from .template_rules import validate_template_rules
            validate_template_rules(template_rules)
            if template_rules["category_id"] != manifest["category_id"]:
                raise AssetPackageError("模板规则类别与资产包不一致")
            rules_path = seed / TEMPLATE_RULES_NAME
            write_json_atomic(rules_path, template_rules)
            manifest.update({"template_rules": TEMPLATE_RULES_NAME,
                             "template_rules_sha256": sha256_file(rules_path)})
        elif source.get("schema_version") != PACKAGE_V3_SCHEMA_VERSION:
            rules_path = write_builtin_template_rules(
                seed / TEMPLATE_RULES_NAME,
                str(manifest["category_id"]),
            )
            manifest.update({
                "template_rules": TEMPLATE_RULES_NAME,
                "template_rules_sha256": sha256_file(rules_path),
            })
        manifest.update({
            "schema_version": PACKAGE_V3_SCHEMA_VERSION,
            "compatibility": {
                "reader": "mvp_v2",
                "legacy_v1_readable": True,
                "legacy_v2_readable": True,
            },
        })
        write_json_atomic(manifest_path, manifest)
        os.replace(seed, target)
        return target / MANIFEST_NAME
    except Exception:
        if seed.exists():
            shutil.rmtree(seed)
        raise


def _project_unpublished_source_semantics(manifest_path: Path) -> None:
    """Enrich a newly built candidate before registration; never mutate a source version."""
    from .ai_spec import SEMANTIC_COLUMNS, refresh_ai_spec
    from .excel_io import _sheet_overview_cells
    from .template_rules import validate_template_rules
    from openpyxl import load_workbook

    package = load_asset_package(manifest_path)
    workbook_path = package["source_workbook_path"]
    original_sha = sha256_file(workbook_path)
    # Refresh outside the immutable-package tree, then copy only this candidate's
    # portable normalized projection. The source version remains untouched.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="mplus-semantic-projection-") as temporary:
        staging = Path(temporary)
        staged_workbook = staging / workbook_path.name
        shutil.copy2(workbook_path, staged_workbook)
        report = refresh_ai_spec(staged_workbook)
        if report.in_sync and report.usable:
            staged_copy = workbook_path.with_name(".semantic-" + workbook_path.name)
            shutil.copy2(staged_workbook, staged_copy)
            os.replace(staged_copy, workbook_path)
            extracted = staging / "_mvp_v2_extracted_images"
            if extracted.is_dir():
                shutil.copytree(extracted, workbook_path.parent / extracted.name, dirs_exist_ok=True)
    if not report.in_sync or not report.usable:
        raise AssetPackageError("语义候选的 AI 规范表未就绪，请先完成来源复核")
    projected = {(row.source_sheet, row.source_row): row for row in report.rows}
    index = package["index_path"]
    index_mode = index.stat().st_mode & 0o777
    # Snapshot the copied SQLite database so inherited WAL files cannot hide edits.
    snapshot = index.with_name(".semantic-index.sqlite3")
    with _open_readonly_index(index) as reader, sqlite3.connect(snapshot) as writer:
        reader.backup(writer)
        writer.execute("PRAGMA journal_mode=DELETE")
    for suffix in ("-wal", "-shm"):
        index.with_name(index.name + suffix).unlink(missing_ok=True)
    os.replace(snapshot, index)
    with sqlite3.connect(index) as connection:
        rows = connection.execute("SELECT asset_id, payload_json FROM script_assets").fetchall()
        if len(rows) != len(projected):
            raise AssetPackageError("原知识语义候选来源行数量不一致")
        for asset_id, raw in rows:
            payload = json.loads(raw)
            row = projected.get((payload["source_sheet"], payload["source_row"]))
            if row is None:
                raise AssetPackageError("原知识语义候选缺少明确来源行")
            # Preserve packaged image paths, IDs, reviewed safety fields and all prior content.
            payload.update({column: getattr(row, column) for column in SEMANTIC_COLUMNS})
            shot = SourceShot(**{key: value for key, value in payload.items() if key != "record_type"})
            connection.execute("UPDATE script_assets SET payload_json=?, source_sha256=?, searchable_text=? WHERE asset_id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), sha256_json(payload), shot.searchable_text(), asset_id))
        _rebuild_fts(connection)
        digests = script_payload_digest_manifest(connection)
    index.chmod(index_mode)

    rules = json.loads(json.dumps(package["template_rule_contract"]["payload"]))
    template_rows = sorted((row for row in report.rows if row.record_type == "template"), key=lambda row: row.slot_index)
    for rule in rules["rules"]:
        selector = rule["selector"]
        selected = [row for row in template_rows if selector["slot_start"] <= row.slot_index <= selector["slot_end"]]
        if not selected or len({row.source_sheet for row in selected}) != 1:
            raise AssetPackageError("当前结构规则无法绑定实际来源槽位，请核对模板")
        selector["source_sheet"] = selected[0].source_sheet
        selector["sources"] = [{"source_file": row.source_file, "source_sheet": row.source_sheet,
                                "source_row": row.source_row, "slot_index": row.slot_index} for row in selected]
    validate_template_rules(rules)
    rules_path = package["package_root"] / TEMPLATE_RULES_NAME
    write_json_atomic(rules_path, rules)

    workbook = load_workbook(workbook_path, read_only=True, data_only=False)
    try:
        sheets = [{"source_sheet": name, "overview_cells": _sheet_overview_cells(workbook[name]),
                   "interpretation": "原文参考；示例、约数、条件与例外按原义保留，不自动成为硬约束"}
                  for name in ("模版", "创意库") if name in workbook.sheetnames]
    finally:
        workbook.close()
    context = {"schema_version": "mplus-source-semantics/v1", "source_file": workbook_path.name,
        "workbook_sha256": sha256_file(workbook_path), "original_workbook_sha256": original_sha,
        "sheets": sheets, "structure_policy": "沿用已登记结构；原文差异仅供核对，明确调整后才改变结构",
        "registered_structure": [{"value": rule["value"], "selector": rule["selector"]}
            for rule in rules["rules"] if rule["field"] == "structure"]}
    semantic_path = package["package_root"] / "source_semantics.json"
    write_json_atomic(semantic_path, context)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stat = workbook_path.stat()
    manifest.update({"source_semantics_file": semantic_path.name, "source_semantics_sha256": sha256_file(semantic_path),
        "source_workbook_sha256": context["workbook_sha256"], "source_workbook_size": stat.st_size,
        "source_workbook_mtime_ns": stat.st_mtime_ns, "index_sha256": sha256_file(index),
        "script_payload_digests": digests, "index_source_view": "ai-spec",
        "template_rules": rules_path.name, "template_rules_sha256": sha256_file(rules_path)})
    # The source version is the recovery copy; these are refresh's temporary candidate files.
    for suffix in (".ai-spec.bak", ".ai-spec.lock"):
        Path(str(workbook_path) + suffix).unlink(missing_ok=True)
    write_json_atomic(manifest_path, manifest)


def asset_search_queries(query: str) -> tuple[str, ...]:
    """Expand established wording equivalents for lookup, never source facts.

    合影/合照 already share an event family in generator and visual_function.
    Do not broaden this to related but distinct actions (e.g. 签名/签字).
    """
    text = " ".join(str(query or "").split())
    return tuple(dict.fromkeys((text, text.replace("合影", "合照"),
                                text.replace("合照", "合影"))))


def search_asset_package(
    path: str | Path,
    query: str,
    *,
    kind: str = "script",
    limit: int = 10,
) -> list[dict]:
    package = load_asset_package(path)
    connection = _open_readonly_index(package["index_path"])
    connection.row_factory = sqlite3.Row
    try:
        matches, seen = [], set()
        for normalized in asset_search_queries(query):
            if kind == "script":
                if normalized:
                    rows = connection.execute(
                        """SELECT s.* FROM script_fts f JOIN script_assets s ON s.rowid=f.rowid
                           WHERE script_fts MATCH ? ORDER BY bm25(script_fts) LIMIT ?""",
                        (_fts_query(normalized), limit),
                    ).fetchall()
                    if not rows:
                        rows = connection.execute(
                            "SELECT * FROM script_assets WHERE searchable_text LIKE ? ORDER BY source_sheet, source_row LIMIT ?",
                            (f"%{normalized}%", limit),
                        ).fetchall()
                else:
                    rows = connection.execute("SELECT * FROM script_assets ORDER BY source_sheet, source_row LIMIT ?", (limit,)).fetchall()
            elif kind == "media":
                if normalized:
                    rows = connection.execute(
                        """SELECT m.* FROM media_fts f JOIN media_assets m ON m.rowid=f.rowid
                           WHERE media_fts MATCH ? ORDER BY bm25(media_fts) LIMIT ?""",
                        (_fts_query(normalized), limit),
                    ).fetchall()
                    if not rows:
                        rows = connection.execute(
                            "SELECT * FROM media_assets WHERE searchable_text LIKE ? OR filename LIKE ? ORDER BY filename LIMIT ?",
                            (f"%{normalized}%", f"%{normalized}%", limit),
                        ).fetchall()
                else:
                    rows = connection.execute("SELECT * FROM media_assets ORDER BY filename LIMIT ?", (limit,)).fetchall()
            else:
                raise AssetPackageError(f"未知索引类型: {kind}")
            for row in rows:
                identity = row["asset_id" if kind == "script" else "media_id"]
                if identity not in seen:
                    seen.add(identity)
                    matches.append(row)
            if limit >= 0 and len(matches) >= limit:
                break
        rows = matches if limit < 0 else matches[:limit]
        results = []
        for row in rows:
            item = dict(row)
            if kind == "script":
                verified = verify_script_payload_digest(package.get("script_payload_digests"),
                    str(item["asset_id"]), json.loads(item["payload_json"]), source_sha256=str(item["source_sha256"]))
                item.update(source_record_sha256=str(item["source_sha256"]), digest_scope="source_record",
                    packaged_payload_sha256=(package["script_payload_digests"]["records"][str(item["asset_id"])]["packaged_payload_sha256"]
                                             if verified else ""), packaged_payload_verified=verified)
            if kind == "media":
                links = connection.execute(
                    """SELECT reference_kind, source_file, source_sheet, source_row,
                              structure, shot_no, visual_desc, shot_function,
                              visual_subject, tags, source_brand
                       FROM media_script_links WHERE media_id=?
                       ORDER BY source_sheet, source_row, reference_kind""",
                    (item["media_id"],),
                ).fetchall()
                item["source_links"] = [dict(link) for link in links]
                item["semantic_basis"] = (
                    "vision+source_row" if item.get("analysis_json") and links
                    else "vision" if item.get("analysis_json")
                    else "source_row" if links
                    else "filename"
                )
            results.append(item)
        return results
    finally:
        connection.close()


def package_media_for_brief(
    path: str | Path,
    brief: dict,
    *,
    expected_category_id: str | None = None,
) -> tuple[list[dict], list[dict]]:
    package = load_asset_package(path)
    script_type = str(brief.get("脚本类型") or "").strip()
    if script_type:
        expected_category_id = category_id_for_script_type(script_type)
    if expected_category_id and package.get("category_id") != expected_category_id:
        raise AssetPackageError(
            f"资产包类别 {package.get('category_id')} 与当前运行类别 "
            f"{expected_category_id} 不一致"
        )
    connection = _open_readonly_index(package["index_path"])
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM media_assets ORDER BY current_path"
        ).fetchall()
        links_by_media_id: dict[str, list[dict]] = {}
        for link in connection.execute(
            """SELECT media_id, reference_kind, source_file, source_sheet, source_row,
                      structure, shot_no, visual_desc, shot_function, visual_subject,
                      tags, source_brand
               FROM media_script_links
               WHERE package_id=?
               ORDER BY source_sheet, source_row, reference_kind""",
            (package["package_id"],),
        ).fetchall():
            item = dict(link)
            links_by_media_id.setdefault(str(item.pop("media_id")), []).append(item)
        locations_by_media_id: dict[str, list[str]] = {}
        for media_id, location in connection.execute(
            "SELECT media_id, path FROM media_locations ORDER BY path"
        ).fetchall():
            locations_by_media_id.setdefault(str(media_id), []).append(str(location))
    finally:
        connection.close()
    hints = _brief_media_hints(brief)
    manifest_rows = []
    analysis_rows = []
    for row in rows:
        item = dict(row)
        image_id = str(item["media_id"])
        source_links = links_by_media_id.get(image_id, [])
        searchable = _normalize_hint(" ".join([
            str(item.get("current_path") or ""),
            str(item.get("searchable_text") or ""),
            str(item.get("brand_names_json") or ""),
        ]))
        # 已审核源表参考图属于当前类别的脚本证据，不应按新项目品牌过滤。
        # 只有未与源行关联的外部素材才使用 Brief hint 缩小范围。
        if not source_links and hints and not any(hint in searchable for hint in hints):
            continue
        current_path = _runtime_package_path(package, item["current_path"])
        alternate_paths = [
            _runtime_package_path(package, value)
            for value in locations_by_media_id.get(image_id, [current_path])
        ]
        source = source_links[0] if source_links else {}
        semantic_text = " ".join(
            str(link.get(key) or "").strip()
            for link in source_links
            for key in (
                "structure", "visual_desc", "shot_function", "visual_subject",
                "tags", "source_brand",
            )
            if str(link.get(key) or "").strip()
        )
        manifest_rows.append({
            "image_id": image_id,
            "项目名": Path(current_path).parent.name,
            "原图路径": current_path,
            "预览路径": current_path,
            "来源文件": source.get("source_file") or package["package_id"],
            "来源 sheet": source.get("source_sheet") or "media_assets",
            "来源行": source.get("source_row") or image_id,
            "是否低清": False,
            "asset_package_id": package["package_id"],
            "asset_role": "source_reference" if source_links else "external_media",
            "analysis_required": not bool(source_links),
            "semantic_basis": (
                "vision+source_row" if item.get("analysis_json") and source_links
                else "vision" if item.get("analysis_json")
                else "source_row" if source_links
                else "filename"
            ),
            "source_semantic_text": semantic_text,
            "source_links": source_links,
            "alternate_paths": alternate_paths,
        })
        if item.get("analysis_json"):
            analysis = json.loads(str(item["analysis_json"]))
            analysis.update({
                "image_id": image_id,
                "asset_package_id": package["package_id"],
                "content_sha256": item["content_sha256"],
            })
            analysis_rows.append(analysis)
    return manifest_rows, analysis_rows


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS script_assets(
            asset_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, category_id TEXT NOT NULL,
            source_file TEXT NOT NULL, source_sheet TEXT NOT NULL, source_row INTEGER NOT NULL,
            structure TEXT, shot_no TEXT, visual_desc TEXT, searchable_text TEXT NOT NULL,
            source_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS media_assets(
            media_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, content_sha256 TEXT NOT NULL,
            current_path TEXT NOT NULL, filename TEXT NOT NULL, extension TEXT,
            size_bytes INTEGER, mtime_ns INTEGER, caption TEXT, tags_json TEXT,
            ocr_text TEXT, brand_names_json TEXT, scene_type TEXT, shot_type TEXT,
            action_type TEXT, review_status TEXT, analysis_json TEXT,
            searchable_text TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS media_content_sha256_idx ON media_assets(content_sha256);
        CREATE TABLE IF NOT EXISTS media_locations(
            path TEXT PRIMARY KEY, media_id TEXT NOT NULL, size_bytes INTEGER,
            mtime_ns INTEGER, last_seen_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS media_locations_media_idx ON media_locations(media_id);
        CREATE TABLE IF NOT EXISTS media_script_links(
            media_id TEXT NOT NULL, asset_id TEXT NOT NULL, package_id TEXT NOT NULL,
            category_id TEXT NOT NULL, reference_kind TEXT NOT NULL,
            source_file TEXT NOT NULL, source_sheet TEXT NOT NULL, source_row INTEGER NOT NULL,
            structure TEXT, shot_no TEXT, visual_desc TEXT, shot_function TEXT,
            visual_subject TEXT, tags TEXT, source_brand TEXT,
            searchable_text TEXT NOT NULL,
            PRIMARY KEY(media_id, asset_id, reference_kind)
        );
        CREATE INDEX IF NOT EXISTS media_script_links_media_idx ON media_script_links(media_id);
        CREATE INDEX IF NOT EXISTS media_script_links_source_idx
            ON media_script_links(package_id, source_sheet, source_row);
        CREATE TABLE IF NOT EXISTS metadata_sources(
            analysis_path TEXT PRIMARY KEY, manifest_path TEXT NOT NULL,
            analysis_size INTEGER NOT NULL, analysis_mtime_ns INTEGER NOT NULL,
            manifest_size INTEGER NOT NULL, manifest_mtime_ns INTEGER NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS script_fts USING fts5(searchable_text, content='script_assets', content_rowid='rowid');
        CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(searchable_text, filename, content='media_assets', content_rowid='rowid');
    """)
    media_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(media_assets)").fetchall()
    }
    if "mtime_ns" not in media_columns:
        connection.execute("ALTER TABLE media_assets ADD COLUMN mtime_ns INTEGER")
    if "storage_ref" not in media_columns:
        connection.execute("ALTER TABLE media_assets ADD COLUMN storage_ref TEXT")
    for name, declaration in (
        ("caption", "TEXT"), ("tags_json", "TEXT"), ("ocr_text", "TEXT"),
        ("brand_names_json", "TEXT"), ("scene_type", "TEXT"), ("shot_type", "TEXT"),
        ("action_type", "TEXT"), ("review_status", "TEXT"), ("analysis_json", "TEXT"),
    ):
        if name not in media_columns:
            connection.execute(f"ALTER TABLE media_assets ADD COLUMN {name} {declaration}")


def _rebuild_fts(connection: sqlite3.Connection) -> None:
    connection.execute("INSERT INTO script_fts(script_fts) VALUES('rebuild')")
    connection.execute("INSERT INTO media_fts(media_fts) VALUES('rebuild')")
    connection.commit()


def _collect_media_paths(shots, media_roots: list[Path]) -> list[Path]:
    paths: dict[str, Path] = {}
    for shot in shots:
        for _, candidate in _shot_media_paths(shot):
            paths[str(candidate)] = candidate
    for root in media_roots:
        if not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            if candidate.is_file() and candidate.suffix.casefold() in IMAGE_EXTENSIONS:
                paths[str(candidate.resolve())] = candidate.resolve()
    return list(paths.values())


def _shot_media_paths(shot: SourceShot) -> list[tuple[str, Path]]:
    paths = []
    for reference_kind, value in (
        ("reference", shot.reference_image_path),
        ("effect", shot.effect_reference_path),
    ):
        for item in str(value or "").split(";"):
            candidate = Path(item.strip()).expanduser()
            if candidate.is_file() and candidate.suffix.casefold() in IMAGE_EXTENSIONS:
                paths.append((reference_kind, candidate.resolve()))
    return paths


def _index_source_media_links(
    connection: sqlite3.Connection,
    package: dict,
    shot_records: list[tuple[str, SourceShot]],
) -> None:
    for _, shot in shot_records:
        asset_id = _script_asset_id(package["package_id"], shot.source_sheet, shot.source_row)
        searchable = shot.searchable_text().strip()
        for reference_kind, media_path in _shot_media_paths(shot):
            location = connection.execute(
                "SELECT media_id FROM media_locations WHERE path=?",
                (str(media_path),),
            ).fetchone()
            if not location:
                continue
            connection.execute(
                """INSERT INTO media_script_links(
                    media_id, asset_id, package_id, category_id, reference_kind,
                    source_file, source_sheet, source_row, structure, shot_no,
                    visual_desc, shot_function, visual_subject, tags, source_brand,
                    searchable_text
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(media_id, asset_id, reference_kind) DO UPDATE SET
                    source_file=excluded.source_file, source_sheet=excluded.source_sheet,
                    source_row=excluded.source_row, structure=excluded.structure,
                    shot_no=excluded.shot_no, visual_desc=excluded.visual_desc,
                    shot_function=excluded.shot_function,
                    visual_subject=excluded.visual_subject, tags=excluded.tags,
                    source_brand=excluded.source_brand,
                    searchable_text=excluded.searchable_text""",
                (
                    str(location[0]), asset_id, package["package_id"],
                    package["category_id"], reference_kind, shot.source_file,
                    shot.source_sheet, shot.source_row, shot.structure, shot.shot_no,
                    shot.visual_desc, shot.shot_function, shot.visual_subject,
                    shot.tags, shot.source_brand, searchable,
                ),
            )


def _refresh_media_searchable_text(connection: sqlite3.Connection, package_id: str) -> None:
    rows = connection.execute(
        """SELECT media_id, filename, caption, tags_json, ocr_text,
                  brand_names_json, scene_type, shot_type, action_type
           FROM media_assets WHERE package_id=?""",
        (package_id,),
    ).fetchall()
    for row in rows:
        links = connection.execute(
            "SELECT searchable_text FROM media_script_links WHERE media_id=?",
            (row[0],),
        ).fetchall()
        searchable = " ".join(
            str(value or "").strip()
            for value in (
                Path(str(row[1] or "")).stem.replace("_", " ").replace("-", " "),
                *row[2:],
                *(link[0] for link in links),
            )
            if str(value or "").strip()
        )
        connection.execute(
            "UPDATE media_assets SET searchable_text=?, updated_at=? WHERE media_id=?",
            (searchable, _utc_now(), row[0]),
        )


def _apply_analysis_metadata(connection: sqlite3.Connection, metadata_roots: list[Path]) -> dict:
    latest_by_path: dict[str, tuple[float, dict]] = {}
    read_count = 0
    reused_count = 0
    for root in metadata_roots:
        if not root.is_dir():
            continue
        for manifest_path in root.rglob("image_manifest.jsonl"):
            analysis_path = manifest_path.with_name("image_analysis.minimax.jsonl")
            if not analysis_path.is_file():
                continue
            manifest_stat = manifest_path.stat()
            analysis_stat = analysis_path.stat()
            checkpoint = connection.execute(
                """SELECT analysis_size, analysis_mtime_ns, manifest_size, manifest_mtime_ns
                   FROM metadata_sources WHERE analysis_path=?""",
                (str(analysis_path.resolve()),),
            ).fetchone()
            signature = (
                analysis_stat.st_size, analysis_stat.st_mtime_ns,
                manifest_stat.st_size, manifest_stat.st_mtime_ns,
            )
            if checkpoint and tuple(map(int, checkpoint)) == signature:
                reused_count += 1
                continue
            read_count += 1
            analysis_by_id = {
                str(row.get("image_id") or ""): row
                for row in _read_jsonl(analysis_path)
                if row.get("image_id")
            }
            updated_at = analysis_stat.st_mtime
            for manifest_row in _read_jsonl(manifest_path):
                image_id = str(manifest_row.get("image_id") or "")
                original_value = str(manifest_row.get("原图路径") or "").strip()
                analysis = analysis_by_id.get(image_id)
                if not analysis or not original_value:
                    continue
                original = Path(original_value)
                key = str(original.expanduser().resolve())
                if key not in latest_by_path or latest_by_path[key][0] < updated_at:
                    latest_by_path[key] = (updated_at, analysis)
            connection.execute(
                """INSERT INTO metadata_sources(
                   analysis_path, manifest_path, analysis_size, analysis_mtime_ns,
                   manifest_size, manifest_mtime_ns, indexed_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(analysis_path) DO UPDATE SET
                   manifest_path=excluded.manifest_path,
                   analysis_size=excluded.analysis_size,
                   analysis_mtime_ns=excluded.analysis_mtime_ns,
                   manifest_size=excluded.manifest_size,
                   manifest_mtime_ns=excluded.manifest_mtime_ns,
                   indexed_at=excluded.indexed_at""",
                (
                    str(analysis_path.resolve()), str(manifest_path.resolve()),
                    *signature, _utc_now(),
                ),
            )
    updated_media_ids = set()
    for path, (_, analysis) in latest_by_path.items():
        location = connection.execute(
            "SELECT media_id FROM media_locations WHERE path=?", (path,)
        ).fetchone()
        if not location:
            continue
        media_id = str(location[0])
        _write_media_analysis(connection, media_id, analysis)
        updated_media_ids.add(media_id)
    return {"read": read_count, "reused": reused_count, "updated_media": len(updated_media_ids)}


def _write_media_analysis(connection: sqlite3.Connection, media_id: str, analysis: dict) -> None:
    """Shared index writer; callers must resolve and verify the media identity first."""
    fields = {
        "caption": str(analysis.get("caption") or ""),
        "tags_json": json.dumps(analysis.get("tags") or [], ensure_ascii=False),
        "ocr_text": str(analysis.get("ocr_text") or ""),
        "brand_names_json": json.dumps(analysis.get("brand_names") or [], ensure_ascii=False),
        "scene_type": str(analysis.get("scene_type") or ""),
        "shot_type": str(analysis.get("shot_type") or ""),
        "action_type": str(analysis.get("action_type") or ""),
        "review_status": str(analysis.get("review_status") or ""),
        "analysis_json": json.dumps(analysis, ensure_ascii=False, sort_keys=True),
    }
    searchable = " ".join([
        fields["caption"], " ".join(map(str, analysis.get("tags") or [])),
        fields["ocr_text"], " ".join(map(str, analysis.get("brand_names") or [])),
        fields["scene_type"], fields["shot_type"], fields["action_type"],
    ]).strip()
    connection.execute(
        """UPDATE media_assets SET caption=?, tags_json=?, ocr_text=?, brand_names_json=?,
           scene_type=?, shot_type=?, action_type=?, review_status=?, analysis_json=?,
           searchable_text=?, updated_at=? WHERE media_id=?""",
        (*fields.values(), searchable, _utc_now(), media_id),
    )


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []
    return rows


def _write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _script_asset_id(package_id: str, sheet: str, row: int) -> str:
    digest = hashlib.sha256(f"{package_id}\0{sheet}\0{row}".encode("utf-8")).hexdigest()
    return f"script_{digest[:20]}"


def _portable_path(package_root: Path, target: Path) -> str:
    try:
        return str(target.relative_to(package_root))
    except ValueError:
        return str(target)


def _resolve_package_path(package_root: Path, value) -> Path:
    path = Path(str(value or ""))
    return (package_root / path).resolve() if not path.is_absolute() else path.resolve()


def _runtime_package_path(package: dict, value) -> str:
    path = Path(str(value or ""))
    if package.get("schema_version") == PACKAGE_V2_SCHEMA_VERSION and not path.is_absolute():
        return str((Path(package["package_root"]) / path).resolve())
    return str(path)


def _runtime_package_reference_paths(package: dict, value) -> str:
    return ";".join(
        _runtime_package_path(package, item.strip())
        for item in str(value or "").split(";")
        if item.strip()
    )


def _fts_query(query: str) -> str:
    tokens = [token.replace('"', '""') for token in query.split() if token]
    return " OR ".join(f'"{token}"' for token in tokens)


def _brief_media_hints(brief: dict) -> list[str]:
    values = [
        str(brief.get("品牌或客户") or ""),
        str(brief.get("项目名称") or ""),
    ]
    return [value for value in (_normalize_hint(item) for item in values) if value]


def _normalize_hint(value: str) -> str:
    text = "".join(str(value or "").casefold().split())
    for suffix in ("发布会开场短片", "发布会脚本", "发布会", "开场短片", "脚本"):
        text = text.replace(suffix, "")
    return text


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
