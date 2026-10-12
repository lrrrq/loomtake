from __future__ import annotations

from pathlib import Path, PurePosixPath

from .cloud_runtime import CloudRuntimeError
from .cloud_storage import CosObjectStorage
from .package_registry import build_package_record


def upload_package(
    package_path: str | Path,
    storage: CosObjectStorage,
    repository,
    *,
    promote_formal: bool = False,
) -> dict:
    """Upload one immutable package and register the exact COS-backed version."""
    record = build_package_record(package_path)
    return sync_packages(
        [record],
        storage,
        repository,
        promote_formal=promote_formal,
    )[0]


def sync_packages(
    records: list[dict],
    storage: CosObjectStorage,
    repository,
    *,
    promote_formal: bool = False,
) -> list[dict]:
    """Preflight every version, upload every object, then atomically promote aliases."""
    if not records:
        return []
    with repository.package_sync_lock():
        repository.preflight_packages(records)
        uploaded = [_upload_record(record, storage) for record in records]
        stored = repository.register_packages([item["cloud_record"] for item in uploaded])
        aliases = [None] * len(records)
        if promote_formal:
            aliases = repository.promote_aliases([
                {
                    "category_id": record["category_id"],
                    "package_id": record["package_id"],
                    "package_version": record["package_version"],
                    "alias": "formal",
                }
                for record in records
            ])
    return [
        {
            "status": "registered",
            "package": stored[index],
            "alias": aliases[index],
            "uploaded_file_count": len(item["uploaded"]),
            "uploaded_size_bytes": sum(row["size_bytes"] for row in item["uploaded"]),
        }
        for index, item in enumerate(uploaded)
    ]


def _upload_record(record: dict, storage: CosObjectStorage) -> dict:
    root = Path(record["local_path"])
    prefix = storage.key(
        f"packages/{record['package_id']}/{record['package_version']}/{record['package_sha256']}"
    )
    uploaded = []
    for file_record in record["files"]:
        relative = _safe_relative(file_record["path"])
        cloud = storage.put_file(
            root.joinpath(*relative.parts),
            key=f"{prefix}/{relative.as_posix()}",
            expected_sha256=file_record["sha256"],
        )
        uploaded.append({**file_record, "storage_ref": cloud.storage_ref})
    cloud_record = {
        **record,
        "storage_prefix": prefix,
        "file_records": uploaded,
    }
    return {"cloud_record": cloud_record, "uploaded": uploaded}


def materialize_package(record: dict, storage: CosObjectStorage, cache_root: str | Path) -> Path:
    package_sha = str(record.get("package_sha256") or "")
    package_id = str(record.get("package_id") or "")
    package_version = record.get("package_version")
    files = list(record.get("package_files") or record.get("file_records") or [])
    if not package_sha or not package_id or type(package_version) is not int or not files:
        raise CloudRuntimeError("云端 run 缺少已锁定的资产包文件清单")
    target = Path(cache_root).expanduser().resolve() / package_id / str(package_version) / package_sha
    for item in files:
        relative = _safe_relative(item.get("path"))
        storage.download_file(
            item.get("storage_ref") or "",
            target.joinpath(*relative.parts),
            expected_sha256=item.get("sha256") or "",
        )
    manifest = target / "asset_package.json"
    if not manifest.is_file():
        raise CloudRuntimeError("云端资产包缺少 asset_package.json")
    return target


def _safe_relative(value: object) -> PurePosixPath:
    text = str(value or "")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CloudRuntimeError("资产包云端路径无效")
    return path
