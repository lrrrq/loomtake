from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import uuid
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo

from .artifact_service import RunArtifactResolver
from .runtime_config import configured_source_commit
from .workspace_store import _MIGRATIONS


SCHEMA_VERSION = "mplus-cloud-data-snapshot/v2"
LEGACY_SCHEMA_VERSION = "mplus-cloud-data-snapshot/v1"
MANIFEST_NAME = "migration-manifest.json"
DATA_PREFIX = "data"
_DATABASE_NAME = "workspace.sqlite3"
_SKIPPED_NAMES = {
    ".DS_Store",
    ".pipeline.lock",
    "cloud-migration.lock",
    "workspace.sqlite3-shm",
    "workspace.sqlite3-wal",
}


class CloudMigrationError(RuntimeError):
    pass


def create_cloud_snapshot(
    data_root: str | Path,
    archive_path: str | Path,
    *,
    source_root: str | Path,
    asset_packages: tuple[str | Path, ...] = (),
    source_commit: str | None = None,
) -> dict:
    root = _required_directory(data_root, "data_root")
    archive = Path(archive_path).expanduser().resolve()
    if archive.exists():
        raise CloudMigrationError("目标快照已存在，拒绝覆盖")
    if _is_within(archive, root):
        raise CloudMigrationError("快照文件不能写入被打包的 data_root")
    database = root / _DATABASE_NAME
    if not database.is_file():
        raise CloudMigrationError("data_root 缺少 workspace.sqlite3")
    _require_runtime_dependency_inside(root, source_root, "source_root", directory=True)
    for package in asset_packages:
        if str(package or "").strip():
            _require_runtime_dependency_inside(root, package, "asset_package", directory=False)

    commit = str(source_commit or configured_source_commit()).strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise CloudMigrationError("导出必须绑定完整的 40 位 source commit")

    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix="mplus-cloud-export-"))
    snapshot_root = temporary_root / DATA_PREFIX
    snapshot_root.mkdir()
    temporary_archive = archive.parent / f".{archive.name}.{uuid.uuid4().hex}.tmp"
    try:
        before = _source_records(root)
        _copy_regular_files(root, snapshot_root, before)
        _backup_database(database, snapshot_root / _DATABASE_NAME)
        after = _source_records(root)
        if before != after:
            raise CloudMigrationError("导出期间 data_root 发生变化，请停止服务后重试")

        database_report = _database_report(snapshot_root / _DATABASE_NAME)
        expected_migrations = [version for version, _name, _statements in _MIGRATIONS]
        if database_report["schema_migrations"] != expected_migrations:
            raise CloudMigrationError(
                "workspace.sqlite3 迁移版本与当前源码不一致，拒绝生成云快照"
            )
        files = _snapshot_records(snapshot_root)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": f"snapshot-{uuid.uuid4().hex}",
            "created_at": _utc_now(),
            "source_commit": commit,
            "source_root_relative": _relative_dependency(root, source_root),
            "asset_packages_relative": [
                _relative_dependency(root, item)
                for item in asset_packages
                if str(item or "").strip()
            ],
            "database": database_report,
            "files": files,
            "file_count": len(files),
            "size_bytes": sum(item["size_bytes"] for item in files),
            "provider_secrets_included": False,
            "restore_policy": "new-empty-target-only",
        }
        with ZipFile(temporary_archive, "w", compression=ZIP_DEFLATED) as bundle:
            bundle.writestr(
                MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            for record in files:
                bundle.write(
                    snapshot_root / record["path"],
                    f"{DATA_PREFIX}/{record['path']}",
                )
        os.chmod(temporary_archive, 0o600)
        result = verify_cloud_snapshot(temporary_archive)
        try:
            os.link(temporary_archive, archive)
        except FileExistsError as exc:
            raise CloudMigrationError("目标快照已存在，拒绝覆盖") from exc
        result.update({
            "archive_path": str(archive),
            "archive_sha256": _sha256_file(archive),
        })
        return result
    finally:
        temporary_archive.unlink(missing_ok=True)
        shutil.rmtree(temporary_root, ignore_errors=True)


def verify_cloud_snapshot(archive_path: str | Path) -> dict:
    archive = Path(archive_path).expanduser().resolve()
    if not archive.is_file():
        raise CloudMigrationError("云迁移快照不存在")
    try:
        with ZipFile(archive) as bundle:
            infos = bundle.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)) or MANIFEST_NAME not in names:
                raise CloudMigrationError("快照成员重复或缺少 migration manifest")
            for info in infos:
                _validate_zip_member(info)
            try:
                manifest = json.loads(bundle.read(MANIFEST_NAME).decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CloudMigrationError("migration manifest 无法读取") from exc
            _validate_manifest(manifest)
            records = {item["path"]: item for item in manifest["files"]}
            expected = {MANIFEST_NAME, *(f"{DATA_PREFIX}/{path}" for path in records)}
            if set(names) != expected:
                raise CloudMigrationError("快照成员与 migration manifest 不一致")
            for relative, record in records.items():
                size_bytes, digest = _zip_member_digest(
                    bundle,
                    f"{DATA_PREFIX}/{relative}",
                )
                if size_bytes != record["size_bytes"] or digest != record["sha256"]:
                    raise CloudMigrationError(f"快照文件完整性校验失败: {relative}")

            temporary_root = Path(tempfile.mkdtemp(prefix="mplus-cloud-verify-"))
            try:
                database_path = temporary_root / _DATABASE_NAME
                _extract_zip_member(
                    bundle,
                    f"{DATA_PREFIX}/{_DATABASE_NAME}",
                    database_path,
                )
                database_report = _database_report(database_path)
            finally:
                shutil.rmtree(temporary_root, ignore_errors=True)
            if database_report != manifest["database"]:
                raise CloudMigrationError("快照数据库统计与 manifest 不一致")
    except BadZipFile as exc:
        raise CloudMigrationError("云迁移快照不是有效 ZIP") from exc
    return {
        "status": "pass",
        "archive_sha256": _sha256_file(archive),
        "snapshot_id": manifest["snapshot_id"],
        "source_commit": manifest["source_commit"],
        "file_count": manifest["file_count"],
        "size_bytes": manifest["size_bytes"],
        "database": manifest["database"],
    }


def restore_cloud_snapshot(
    archive_path: str | Path,
    target_data_root: str | Path,
    *,
    source_commit: str | None = None,
) -> dict:
    archive = Path(archive_path).expanduser().resolve()
    target = Path(target_data_root).expanduser().resolve()
    if target.exists():
        raise CloudMigrationError("目标 data_root 已存在，拒绝覆盖或重复恢复")
    if target == Path(target.anchor) or target == Path.home().resolve():
        raise CloudMigrationError("目标 data_root 范围过大")
    verification = verify_cloud_snapshot(archive)
    current_commit = str(source_commit or configured_source_commit()).strip().lower()
    if current_commit != verification["source_commit"]:
        raise CloudMigrationError("当前源码 SHA 与数据快照不一致，拒绝恢复")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.restore-{uuid.uuid4().hex}"
    if staging.exists():
        raise CloudMigrationError("恢复临时目录已存在")
    staging.mkdir(mode=0o700)
    try:
        with ZipFile(archive) as bundle:
            manifest = json.loads(bundle.read(MANIFEST_NAME).decode("utf-8"))
            for record in manifest["files"]:
                relative = _safe_relative(record["path"])
                destination = staging.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size_bytes = 0
                with destination.open("xb") as handle:
                    with bundle.open(f"{DATA_PREFIX}/{record['path']}") as source:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            handle.write(chunk)
                            digest.update(chunk)
                            size_bytes += len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if (
                    size_bytes != record["size_bytes"]
                    or digest.hexdigest() != record["sha256"]
                ):
                    raise CloudMigrationError(
                        f"恢复文件完整性校验失败: {record['path']}"
                    )
                os.chmod(destination, int(record["mode"], 8))
                if "mtime_ns" in record:
                    os.utime(destination, ns=(destination.stat().st_atime_ns, record["mtime_ns"]))
        source_relative = _safe_dependency_relative(manifest["source_root_relative"])
        if source_relative != ".":
            staging.joinpath(*PurePosixPath(source_relative).parts).mkdir(
                parents=True,
                exist_ok=True,
            )
        legacy_metadata = (
            _restore_legacy_package_timestamps(staging, manifest)
            if manifest["schema_version"] == LEGACY_SCHEMA_VERSION else None
        )
        restored = validate_data_root(staging)
        if restored["database"] != verification["database"]:
            raise CloudMigrationError("恢复数据库统计与源快照不一致")
        if target.exists():
            raise CloudMigrationError("目标 data_root 在恢复期间出现，拒绝覆盖")
        os.replace(staging, target)
        return {
            **verification,
            "status": "restored",
            "target_data_root": str(target),
            "source_root": str(target / manifest["source_root_relative"]),
            "asset_packages": [
                str(target / item) for item in manifest["asset_packages_relative"]
            ],
            "bound_runs": restored["bound_runs"],
            "provider_secrets_restored": False,
            **({"legacy_package_metadata": legacy_metadata} if legacy_metadata is not None else {}),
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _restore_legacy_package_timestamps(root: Path, manifest: dict) -> dict:
    """Recover v1's missing metadata only from matching frozen package evidence.

    Never rewrite package contents or follow a source outside the extracted
    snapshot. Unprovable timestamps remain explicit warnings, not guesses.
    """
    from .asset_package import SUPPORTED_PACKAGE_SCHEMA_VERSIONS, load_asset_package

    files = {record["path"]: record for record in manifest["files"]}
    candidates = set(manifest["asset_packages_relative"])
    candidates.update(name for name in files if PurePosixPath(name).name == "asset_package.json")
    timestamps: dict[str, set[int]] = {}
    warnings = []
    for name in sorted(candidates):
        try:
            payload = json.loads((root / name).read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") not in SUPPORTED_PACKAGE_SCHEMA_VERSIONS:
                continue
            # A copied editable library is not an immutable dependency. Only
            # explicitly declared packages or published versions participate.
            if name not in manifest["asset_packages_relative"] and (
                    type(payload.get("package_version")) is not int or payload["package_version"] < 1):
                continue
            package = load_asset_package(root / name)
            workbook = package["source_workbook_path"]
            relative = workbook.relative_to(root).as_posix()
            record = files.get(relative)
            timestamp = package.get("source_workbook_mtime_ns")
            if (record is None or type(timestamp) is not int or not 0 <= timestamp <= 2**63 - 1
                    or record["sha256"] != package.get("source_workbook_sha256")
                    or record["size_bytes"] != package.get("source_workbook_size")
                    or _sha256_file(workbook) != record["sha256"]):
                raise ValueError("冻结来源大小、SHA或修改时间依据不完整")
            timestamps.setdefault(relative, set()).add(timestamp)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append({"package": name, "reason": str(exc)})
    restored = []
    for relative, values in sorted(timestamps.items()):
        if len(values) != 1:
            warnings.append({"source": relative, "reason": "多个冻结包的来源修改时间冲突，未修改"})
            continue
        path = root / relative
        timestamp = next(iter(values))
        os.utime(path, ns=(path.stat().st_atime_ns, timestamp))
        restored.append(relative)
    return {"restored_sources": restored, "warnings": warnings}


def validate_data_root(data_root: str | Path) -> dict:
    root = _required_directory(data_root, "data_root")
    database = root / _DATABASE_NAME
    if not database.is_file():
        raise CloudMigrationError("data_root 缺少 workspace.sqlite3")
    database_report = _database_report(database)
    expected_migrations = [version for version, _name, _statements in _MIGRATIONS]
    if database_report["schema_migrations"] != expected_migrations:
        raise CloudMigrationError("恢复后的 schema migration 序列不完整")
    run_report = _validate_bound_runs(root, database)
    return {
        "status": "pass",
        "data_root": str(root),
        "database": database_report,
        "bound_runs": run_report,
    }


def _validate_bound_runs(root: Path, database: Path) -> dict:
    resolver = RunArtifactResolver(root / "runs")
    checked_runs = 0
    checked_artifacts = 0
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT run_id, manifest_sha256 FROM project_runs ORDER BY run_id"
        ).fetchall()
    for run_id, expected_sha256 in rows:
        manifest = resolver.read_manifest(
            str(run_id),
            expected_sha256=str(expected_sha256),
        )
        artifacts = manifest.get("artifacts") or {}
        if not isinstance(artifacts, dict):
            raise CloudMigrationError("run manifest artifacts 无效")
        for artifact_name in sorted(artifacts):
            resolver.resolve_artifact(str(run_id), artifact_name)
            checked_artifacts += 1
        checked_runs += 1
    return {
        "registered": len(rows),
        "verified": checked_runs,
        "artifacts_verified": checked_artifacts,
    }


def _database_report(path: Path) -> dict:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != "ok" or foreign_keys:
                raise CloudMigrationError("workspace.sqlite3 完整性或外键校验失败")
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts = {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in tables
            }
            migrations = [
                int(row[0])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
    except sqlite3.Error as exc:
        raise CloudMigrationError("workspace.sqlite3 无法校验") from exc
    return {
        "integrity_check": "ok",
        "foreign_key_violations": 0,
        "schema_migrations": migrations,
        "table_counts": counts,
    }


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(source) as source_connection:
            with sqlite3.connect(destination) as target_connection:
                source_connection.backup(target_connection)
    except sqlite3.Error as exc:
        raise CloudMigrationError("workspace.sqlite3 在线备份失败") from exc


def _source_records(root: Path) -> dict[str, tuple[int, str, int, int]]:
    records = {}
    for path in sorted(root.rglob("*")):
        if _skip_source_path(root, path):
            continue
        if path.is_symlink():
            raise CloudMigrationError(f"data_root 含符号链接，拒绝打包: {path.relative_to(root)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise CloudMigrationError(f"data_root 含非普通文件: {path.relative_to(root)}")
        relative = path.relative_to(root).as_posix()
        metadata = path.stat()
        records[relative] = (metadata.st_size, _sha256_file(path), stat.S_IMODE(metadata.st_mode), metadata.st_mtime_ns)
    return records


def _copy_regular_files(
    source_root: Path,
    target_root: Path,
    records: dict[str, tuple[int, str, int, int]],
) -> None:
    for relative, (size_bytes, digest, mode, mtime_ns) in records.items():
        source = source_root / relative
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if (target.stat().st_size != size_bytes or _sha256_file(target) != digest
                or target.stat().st_mtime_ns != mtime_ns):
            raise CloudMigrationError(f"复制后 SHA-256 不一致: {relative}")
        os.chmod(target, mode)


def _snapshot_records(root: Path) -> list[dict]:
    records = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        records.append({
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
            "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
            "mtime_ns": path.stat().st_mtime_ns,
        })
    return records


def _skip_source_path(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    filename = path.name.casefold()
    if ".staging" in relative.parts:
        return True
    if path.name in _SKIPPED_NAMES or path.name == _DATABASE_NAME:
        return True
    if relative.parts and relative.parts[0] == "secrets":
        return relative.as_posix() != "secrets/agent-confirmation.key"
    if (
        filename == ".env"
        or filename.startswith(".env.")
        or filename.endswith(".env")
        or filename == "config.json"
    ):
        return True
    return False


def _validate_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in {SCHEMA_VERSION, LEGACY_SCHEMA_VERSION}:
        raise CloudMigrationError("migration manifest schema 无效")
    expected_fields = {
        "schema_version",
        "snapshot_id",
        "created_at",
        "source_commit",
        "source_root_relative",
        "asset_packages_relative",
        "database",
        "files",
        "file_count",
        "size_bytes",
        "provider_secrets_included",
        "restore_policy",
    }
    if set(manifest) != expected_fields:
        raise CloudMigrationError("migration manifest 字段集合无效")
    snapshot_id = str(manifest.get("snapshot_id") or "")
    if (
        not snapshot_id.startswith("snapshot-")
        or len(snapshot_id) != len("snapshot-") + 32
        or any(char not in "0123456789abcdef" for char in snapshot_id[9:])
    ):
        raise CloudMigrationError("migration manifest snapshot_id 无效")
    try:
        created_at = datetime.strptime(
            str(manifest.get("created_at") or ""),
            "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise CloudMigrationError("migration manifest created_at 无效") from exc
    if created_at.utcoffset() != timezone.utc.utcoffset(created_at):
        raise CloudMigrationError("migration manifest created_at 无效")
    if manifest.get("restore_policy") != "new-empty-target-only":
        raise CloudMigrationError("migration manifest restore policy 无效")
    commit = str(manifest.get("source_commit") or "")
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise CloudMigrationError("migration manifest source commit 无效")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise CloudMigrationError("migration manifest 文件清单为空")
    paths = []
    record_fields = {"path", "size_bytes", "sha256", "mode"}
    if manifest["schema_version"] == SCHEMA_VERSION:
        record_fields.add("mtime_ns")
    for record in files:
        if not isinstance(record, dict) or set(record) != record_fields:
            raise CloudMigrationError("migration manifest 文件记录无效")
        if "mtime_ns" in record and (type(record["mtime_ns"]) is not int
                                     or not 0 <= record["mtime_ns"] <= 2**63 - 1):
            raise CloudMigrationError("migration manifest 文件修改时间无效")
        relative = _safe_relative(record["path"]).as_posix()
        if relative != record["path"] or relative in paths:
            raise CloudMigrationError("migration manifest 路径重复或未规范化")
        if type(record["size_bytes"]) is not int or record["size_bytes"] < 0:
            raise CloudMigrationError("migration manifest 文件大小无效")
        digest = str(record["sha256"])
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise CloudMigrationError("migration manifest SHA-256 无效")
        if (
            not isinstance(record["mode"], str)
            or len(record["mode"]) != 4
            or any(char not in "01234567" for char in record["mode"])
            or int(record["mode"], 8) > 0o777
        ):
            raise CloudMigrationError("migration manifest 文件权限无效")
        paths.append(relative)
    if _DATABASE_NAME not in paths:
        raise CloudMigrationError("migration manifest 缺少 workspace.sqlite3")
    if manifest.get("file_count") != len(files):
        raise CloudMigrationError("migration manifest file_count 不一致")
    if manifest.get("size_bytes") != sum(item["size_bytes"] for item in files):
        raise CloudMigrationError("migration manifest size_bytes 不一致")
    if manifest.get("provider_secrets_included") is not False:
        raise CloudMigrationError("云数据快照不得包含 Provider secrets")
    _safe_dependency_relative(manifest.get("source_root_relative"))
    packages = manifest.get("asset_packages_relative")
    if not isinstance(packages, list):
        raise CloudMigrationError("migration manifest 资产包清单无效")
    for item in packages:
        if _safe_dependency_relative(item) not in paths:
            raise CloudMigrationError("migration manifest 资产包不在快照内")


def _validate_zip_member(info: ZipInfo) -> None:
    relative = _safe_relative(info.filename)
    if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
        raise CloudMigrationError(f"快照含目录或符号链接成员: {relative}")


def _safe_relative(value: str | Path) -> PurePosixPath:
    text = str(value or "")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CloudMigrationError("快照包含不安全路径")
    return path


def _safe_dependency_relative(value: str | Path) -> str:
    text = str(value or "")
    if text == ".":
        return text
    return _safe_relative(text).as_posix()


def _required_directory(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise CloudMigrationError(f"{label} 不是可读目录")
    return path


def _require_runtime_dependency_inside(
    data_root: Path,
    value: str | Path,
    label: str,
    *,
    directory: bool,
) -> None:
    path = Path(value).expanduser().resolve()
    if not _is_within(path, data_root):
        raise CloudMigrationError(f"{label} 位于 data_root 外，云快照无法自包含")
    if directory and not path.is_dir():
        raise CloudMigrationError(f"{label} 目录不存在")
    if not directory and not path.is_file():
        raise CloudMigrationError(f"{label} 文件不存在")


def _relative_dependency(data_root: Path, value: str | Path) -> str:
    return Path(value).expanduser().resolve().relative_to(data_root).as_posix()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_member_digest(bundle: ZipFile, name: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    size_bytes = 0
    with bundle.open(name) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size_bytes += len(chunk)
    return size_bytes, digest.hexdigest()


def _extract_zip_member(bundle: ZipFile, name: str, destination: Path) -> None:
    with bundle.open(name) as source, destination.open("xb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            target.write(chunk)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
