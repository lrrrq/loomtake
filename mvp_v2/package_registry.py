from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
from threading import Lock
from typing import Iterable

from .asset_package import AssetPackageError, asset_package_source_record, load_asset_package
from .run_contract import sha256_file, sha256_json
from .run_state import exclusive_lock, try_exclusive_lock, write_json_atomic


REGISTRY_SCHEMA_VERSION = "mplus-package-registry/v1"
PACKAGE_RECORD_SCHEMA_VERSION = "mplus-package-record/v1"
DEFAULT_ALIAS = "formal"
_EXPECTED_ALIAS_UNSET = object()


_RESOLVED_PACKAGE_CACHE: dict[tuple[str, str], tuple[tuple[tuple, ...], dict]] = {}
_RESOLVED_PACKAGE_CACHE_LOCK = Lock()
_RESOLUTION_LOCKS = tuple(Lock() for _ in range(64))
_VERIFICATION_RECEIPT_SCHEMA = "mplus-package-verification/v1"


class PackageRegistryError(RuntimeError):
    pass


def build_package_record(path: str | Path) -> dict:
    """Build a deterministic, content-bound record without changing the package."""
    try:
        package = load_asset_package(path)
        asset_package_source_record(package["manifest_path"])
    except (AssetPackageError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise PackageRegistryError(str(exc)) from exc
    package_version = package.get("package_version")
    if type(package_version) is not int or package_version < 1:
        raise PackageRegistryError("只有已发布且带正整数 package_version 的资产包可注册")
    root = Path(package["package_root"]).resolve()
    files = _package_files(root)
    _verify_declared_integrity(package, files)
    manifest_path = Path(package["manifest_path"]).resolve()
    record = {
        "schema_version": PACKAGE_RECORD_SCHEMA_VERSION,
        "package_id": str(package.get("package_id") or "").strip(),
        "package_version": package_version,
        "category_id": str(package.get("category_id") or "").strip(),
        "package_schema_version": str(package.get("schema_version") or ""),
        "local_path": str(root),
        "manifest_sha256": sha256_file(manifest_path),
        "files": files,
        "file_count": len(files),
        "size_bytes": sum(item["size_bytes"] for item in files),
    }
    if not record["package_id"] or not record["category_id"]:
        raise PackageRegistryError("资产包缺少 package_id 或 category_id")
    record["package_sha256"] = package_record_sha256(record)
    return record


def package_record_sha256(record: dict) -> str:
    return sha256_json({
        "schema_version": PACKAGE_RECORD_SCHEMA_VERSION,
        "package_id": record.get("package_id"),
        "package_version": record.get("package_version"),
        "category_id": record.get("category_id"),
        "package_schema_version": record.get("package_schema_version"),
        "manifest_sha256": record.get("manifest_sha256"),
        "files": [
            {
                "path": item.get("path"),
                "size_bytes": item.get("size_bytes"),
                "sha256": item.get("sha256"),
            }
            for item in record.get("files") or []
        ],
    })


class LocalPackageRegistry:
    def __init__(self, path: str | Path, *, configured_packages: dict | None = None):
        self.configured_packages = dict(configured_packages or {})
        self.path = Path(path).expanduser().resolve()
        self.lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")

    def register(self, package_path: str | Path) -> dict:
        record = build_package_record(package_path)
        return self.register_records([record])[0]

    def register_records(
        self,
        records: Iterable[dict],
        *,
        promote_formal: bool = False,
    ) -> list[dict]:
        """Preflight and atomically register a package batch and optional aliases."""
        prepared = [dict(record) for record in records]
        if not prepared:
            return []
        seen_keys: set[str] = set()
        seen_categories: set[str] = set()
        for record in prepared:
            key = _package_key(record.get("package_id"), record.get("package_version"))
            category_id = _required(record.get("category_id"), "category_id")
            if key in seen_keys:
                raise PackageRegistryError(f"批次中资产包版本重复: {key}")
            if promote_formal and category_id in seen_categories:
                raise PackageRegistryError(f"批次中 Formal 类别重复: {category_id}")
            if package_record_sha256(record) != record.get("package_sha256"):
                raise PackageRegistryError(f"资产包记录 SHA-256 无效: {key}")
            seen_keys.add(key)
            seen_categories.add(category_id)
        with exclusive_lock(self.lock_path):
            registry = self._read()
            for record in prepared:
                key = _package_key(record["package_id"], record["package_version"])
                existing = dict(registry["packages"].get(key) or {})
                if existing and existing.get("package_sha256") != record["package_sha256"]:
                    raise PackageRegistryError(
                        f"不可变版本冲突: {key} 已绑定其他 SHA-256"
                    )
            changed = False
            for record in prepared:
                key = _package_key(record["package_id"], record["package_version"])
                existing = dict(registry["packages"].get(key) or {})
                if existing:
                    if existing.get("local_path") != record["local_path"]:
                        existing["local_path"] = record["local_path"]
                        registry["packages"][key] = existing
                        changed = True
                    continue
                registry["packages"][key] = record
                changed = True
            if promote_formal:
                history = registry.setdefault("alias_history", [])
                for record in prepared:
                    category_id = record["category_id"]
                    key = _package_key(record["package_id"], record["package_version"])
                    aliases = registry["aliases"].setdefault(category_id, {})
                    previous = aliases.get(DEFAULT_ALIAS)
                    if previous == key:
                        continue
                    aliases[DEFAULT_ALIAS] = key
                    history.append({
                        "category_id": category_id,
                        "alias": DEFAULT_ALIAS,
                        "previous_ref": previous,
                        "current_ref": key,
                        "changed_at": _utc_now(),
                    })
                    changed = True
            if changed:
                registry["updated_at"] = _utc_now()
                write_json_atomic(self.path, registry)
            return [
                dict(registry["packages"][_package_key(item["package_id"], item["package_version"])])
                for item in prepared
            ]

    def promote(
        self,
        category_id: str,
        package_id: str,
        package_version: int,
        *,
        alias: str = DEFAULT_ALIAS,
        expected_current_ref: str | None | object = _EXPECTED_ALIAS_UNSET,
        command_id: str | None = None,
        request_sha256: str | None = None,
    ) -> dict:
        normalized_alias = _alias(alias)
        normalized_category = _required(category_id, "category_id")
        key = _package_key(package_id, package_version)
        if (command_id is None) != (request_sha256 is None):
            raise PackageRegistryError("发布命令和请求摘要必须同时提供")
        if command_id is not None:
            command_id = _required(command_id, "command_id")
            if (expected_current_ref is _EXPECTED_ALIAS_UNSET or not isinstance(request_sha256, str)
                    or len(request_sha256) != 64 or any(char not in "0123456789abcdef" for char in request_sha256)):
                raise PackageRegistryError("发布命令必须绑定原 alias 和有效请求摘要")
        if expected_current_ref is not _EXPECTED_ALIAS_UNSET and expected_current_ref is not None:
            expected_current_ref = _required(expected_current_ref, "expected_current_ref")
        with try_exclusive_lock(self.lock_path) as acquired:
            if not acquired:
                raise PackageRegistryError("alias 正在更新，请稍后恢复原发布命令")
            registry = self._read()
            history = registry.setdefault("alias_history", [])
            if command_id is not None:
                previous_command = next((item for item in history if item.get("command_id") == command_id), None)
                if previous_command is not None:
                    expected = {"category_id": normalized_category, "alias": normalized_alias,
                        "current_ref": key, "request_sha256": request_sha256,
                        "expected_current_ref": expected_current_ref}
                    if any(previous_command.get(name) != value for name, value in expected.items()):
                        raise PackageRegistryError("发布命令已绑定其他请求，拒绝重放")
                    receipt = previous_command.get("promotion_result")
                    if not isinstance(receipt, dict):
                        raise PackageRegistryError("发布命令缺少原结果，需要对账")
                    # A later publication may have advanced the alias. Replay only
                    # the original receipt; never write the old alias back.
                    return deepcopy(receipt)
            record = dict(registry["packages"].get(key) or {})
            if not record:
                raise PackageRegistryError(f"资产包版本未注册: {key}")
            if record.get("category_id") != normalized_category:
                raise PackageRegistryError("别名类别与资产包类别不一致")
            previous = registry["aliases"].setdefault(normalized_category, {}).get(normalized_alias)
            if expected_current_ref is not _EXPECTED_ALIAS_UNSET and previous != expected_current_ref:
                raise PackageRegistryError("alias 已被其他发布改变，拒绝覆盖")
            result = {"category_id": normalized_category, "alias": normalized_alias,
                "previous_ref": previous, **record}
            registry["aliases"].setdefault(normalized_category, {})[normalized_alias] = key
            if previous != key or command_id is not None:
                event = {"category_id": normalized_category, "alias": normalized_alias,
                    "previous_ref": previous, "current_ref": key, "changed_at": _utc_now()}
                if command_id is not None:
                    event.update(command_id=command_id, request_sha256=request_sha256,
                        expected_current_ref=expected_current_ref, promotion_result=deepcopy(result))
                history.append(event)
            registry["updated_at"] = _utc_now()
            # Alias and command receipt share the same atomic registry replacement.
            write_json_atomic(self.path, registry)
        return result

    def resolve_formal(self, category_id: str) -> dict:
        """Resolve the current formal alias as one verified immutable snapshot."""
        normalized_category = _required(category_id, "category_id")
        registry = self._read()
        key = str(
            dict(registry["aliases"].get(normalized_category) or {}).get(DEFAULT_ALIAS)
            or ""
        )
        if not key:
            raise PackageRegistryError(
                f"类别 {normalized_category} 未设置 {DEFAULT_ALIAS} 资产包"
            )
        return self.resolve(normalized_category, key)

    def restore_alias(
        self,
        category_id: str,
        expected_current_ref: str,
        previous_ref: str | None,
        *,
        alias: str = DEFAULT_ALIAS,
    ) -> dict:
        """Compensate one alias change without overwriting a concurrent promotion."""
        normalized_category = _required(category_id, "category_id")
        normalized_alias = _alias(alias)
        expected = _required(expected_current_ref, "expected_current_ref")
        previous = str(previous_ref or "").strip()
        if previous and previous not in self._read().get("packages", {}):
            raise PackageRegistryError("alias 补偿目标资产包未注册")
        with exclusive_lock(self.lock_path):
            registry = self._read()
            aliases = registry["aliases"].setdefault(normalized_category, {})
            if aliases.get(normalized_alias) != expected:
                raise PackageRegistryError("alias 已被其他发布改变，拒绝覆盖")
            if previous:
                aliases[normalized_alias] = previous
            else:
                aliases.pop(normalized_alias, None)
            changed_at = _utc_now()
            registry.setdefault("alias_history", []).append({
                "category_id": normalized_category,
                "alias": normalized_alias,
                "previous_ref": expected,
                "current_ref": previous or None,
                "changed_at": changed_at,
                "reason": "publication_compensation",
            })
            registry["updated_at"] = changed_at
            write_json_atomic(self.path, registry)
        return {
            "category_id": normalized_category,
            "alias": normalized_alias,
            "current_ref": previous or None,
            "restored": True,
        }

    def discard_unpromoted(self, package_id: str, package_version: int, package_sha256: str) -> None:
        """Remove only a failed, unaliased registration created by a publication attempt."""
        key = _package_key(package_id, package_version)
        expected_sha = _required(package_sha256, "package_sha256")
        with exclusive_lock(self.lock_path):
            registry = self._read()
            record = dict(registry["packages"].get(key) or {})
            if not record or record.get("package_sha256") != expected_sha:
                return
            if any(
                key == str(alias_ref or "")
                for aliases in (registry.get("aliases") or {}).values()
                for alias_ref in (dict(aliases or {}).values())
            ):
                raise PackageRegistryError("已被 alias 引用的资产包不得删除")
            del registry["packages"][key]
            registry["updated_at"] = _utc_now()
            write_json_atomic(self.path, registry)

    def resolve(self, category_id: str, package_ref: str) -> dict:
        registry = self._read()
        normalized_category = _required(category_id, "category_id")
        package_id, selector = parse_package_ref(package_ref)
        if selector == DEFAULT_ALIAS:
            key = str(
                dict(registry["aliases"].get(normalized_category) or {}).get(selector)
                or ""
            )
            if not key:
                raise PackageRegistryError(
                    f"类别 {normalized_category} 未设置 {selector} 资产包"
                )
            if package_id and not key.startswith(f"{package_id}@"):
                raise PackageRegistryError("指定 package_id 与 Formal 别名指向不一致")
        else:
            key = _package_key(package_id, int(selector))
        record = dict(registry["packages"].get(key) or {})
        if not record:
            raise PackageRegistryError(f"资产包版本未注册: {key}")
        if record.get("category_id") != normalized_category:
            raise PackageRegistryError("资产包类别与运行类别不一致")
        if package_record_sha256(record) != record.get("package_sha256"):
            raise PackageRegistryError("资产包注册记录 SHA-256 已漂移")
        local_path = Path(str(record.get("local_path") or "")).expanduser()
        if not local_path.is_dir():
            raise PackageRegistryError("已注册资产包本地路径不可用")
        cache_key = (str(local_path.resolve()), str(record["package_sha256"]))
        # Coalesce simultaneous cold reads without holding the global cache lock during I/O.
        stripe = int(sha256_json(cache_key)[:8], 16) % len(_RESOLUTION_LOCKS)
        with _RESOLUTION_LOCKS[stripe]:
            fingerprint = _package_stat_fingerprint(local_path)
            with _RESOLVED_PACKAGE_CACHE_LOCK:
                cached = _RESOLVED_PACKAGE_CACHE.get(cache_key)
            if cached is not None and cached[0] == fingerprint:
                return {**record, "resolved_ref": key}
            receipt = _verification_receipt(self.path, local_path, record, fingerprint)
            if not _read_verification_receipt(self.path, local_path, receipt):
                current = build_package_record(local_path)
                if current["package_sha256"] != record["package_sha256"]:
                    raise PackageRegistryError("资产包内容与注册 SHA-256 不一致")
                if _package_stat_fingerprint(local_path) != fingerprint:
                    raise PackageRegistryError("资产包在完整性校验期间发生变化")
                _write_verification_receipt(self.path, local_path, receipt)
            elif _package_stat_fingerprint(local_path) != fingerprint:
                raise PackageRegistryError("资产包在完整性校验期间发生变化")
            with _RESOLVED_PACKAGE_CACHE_LOCK:
                _RESOLVED_PACKAGE_CACHE[cache_key] = (fingerprint, deepcopy(record))
        return {**record, "resolved_ref": key}

    def verified_file_sha256(self, record: dict, path: Path) -> str | None:
        """Reuse content evidence only while this file matches a verified snapshot."""
        root = Path(str(record.get("local_path") or "")).resolve()
        try:
            relative = path.resolve().relative_to(root).as_posix()
            info = path.lstat()
        except (OSError, ValueError):
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        cache_key = (str(root), str(record.get("package_sha256") or ""))
        with _RESOLVED_PACKAGE_CACHE_LOCK:
            cached = _RESOLVED_PACKAGE_CACHE.get(cache_key)
        if cached is None:
            return None
        fingerprint = (relative, info.st_dev, info.st_ino, info.st_size,
                       info.st_mtime_ns, info.st_ctime_ns)
        if fingerprint not in cached[0]:
            return None
        return next((item["sha256"] for item in cached[1]["files"]
                     if item["path"] == relative), None)

    def configured_record(self, category_id: str) -> dict | None:
        path = self.configured_packages.get(category_id)
        if not path:
            return None
        record = build_package_record(path)
        if record["category_id"] != category_id:
            raise PackageRegistryError("配置模板类别不一致")
        return {**record, "resolved_ref": f"{record['package_id']}@{record['package_version']}"}

    def resolve_evidence(self, category_id: str, evidence: dict) -> dict:
        ref = str(evidence.get("resolved_ref") or evidence.get("template_package_ref") or "")
        expected = str(evidence.get("package_sha256") or evidence.get("template_package_sha256") or "")
        if not ref or not expected:
            raise PackageRegistryError("模板版本证据不完整")
        if (ref not in self._read()["packages"]
                and evidence.get("alias") in {"configured", "legacy-configured"}
                and self.configured_packages.get(category_id)):
            record = build_package_record(self.configured_packages[category_id])
            record = {**record, "resolved_ref": f"{record['package_id']}@{record['package_version']}"}
            if record["resolved_ref"] != ref or record["category_id"] != category_id:
                raise PackageRegistryError("配置模板与已保存版本不一致")
        else:
            record = self.resolve(category_id, ref)
        if record["package_sha256"] != expected:
            raise PackageRegistryError("模板内容与已保存版本不一致")
        return {**record, "alias": str(evidence.get("alias") or "project")}

    def rollback_formal(self, category_ids: Iterable[str] | None = None) -> list[dict]:
        requested = {
            _required(item, "category_id") for item in (category_ids or [])
        }
        with exclusive_lock(self.lock_path):
            registry = self._read()
            categories = sorted(requested or registry["aliases"].keys())
            prepared = []
            history = list(registry.get("alias_history") or [])
            for category_id in categories:
                current = str(
                    dict(registry["aliases"].get(category_id) or {}).get(DEFAULT_ALIAS) or ""
                )
                previous = ""
                for event in reversed(history):
                    if (
                        event.get("category_id") == category_id
                        and event.get("alias") == DEFAULT_ALIAS
                        and event.get("current_ref") == current
                        and event.get("previous_ref")
                    ):
                        previous = str(event["previous_ref"])
                        break
                if not current or previous not in registry["packages"]:
                    raise PackageRegistryError(f"类别 {category_id} 没有可回滚的 Formal 前一版")
                prepared.append((category_id, current, previous))
            changed_at = _utc_now()
            for category_id, current, previous in prepared:
                registry["aliases"].setdefault(category_id, {})[DEFAULT_ALIAS] = previous
                history.append({
                    "category_id": category_id,
                    "alias": DEFAULT_ALIAS,
                    "previous_ref": current,
                    "current_ref": previous,
                    "changed_at": changed_at,
                    "reason": "administrator_rollback",
                })
            registry["alias_history"] = history
            registry["updated_at"] = changed_at
            write_json_atomic(self.path, registry)
            return [
                {
                    "category_id": category_id,
                    "alias": DEFAULT_ALIAS,
                    "previous_ref": current,
                    "current_ref": previous,
                }
                for category_id, current, previous in prepared
            ]

    def list(self) -> dict:
        return self._read()

    def _read(self) -> dict:
        if not self.path.is_file():
            now = _utc_now()
            return {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "created_at": now,
                "updated_at": now,
                "packages": {},
                "aliases": {},
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PackageRegistryError("资产包注册表无法读取") from exc
        if payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise PackageRegistryError("资产包注册表 schema 不受支持")
        if not isinstance(payload.get("packages"), dict) or not isinstance(payload.get("aliases"), dict):
            raise PackageRegistryError("资产包注册表结构无效")
        return payload


def register_formal_packages(
    registry_path: str | Path,
    category_packages: dict[str, str | Path],
    *,
    promote_formal: bool = True,
) -> dict:
    registry = LocalPackageRegistry(registry_path)
    records = []
    for category_id, package_path in category_packages.items():
        record = build_package_record(package_path)
        if record["category_id"] != category_id:
            raise PackageRegistryError(
                f"配置类别 {category_id} 与资产包 {record['category_id']} 不一致"
            )
        records.append(record)
    rows = registry.register_records(records, promote_formal=promote_formal)
    return {"registry_path": str(registry.path), "packages": rows, "count": len(rows)}


def parse_package_ref(value: str) -> tuple[str, str]:
    normalized = _required(value, "package_ref")
    if "@" in normalized:
        package_id, selector = normalized.rsplit("@", 1)
    else:
        package_id, selector = normalized, DEFAULT_ALIAS
    package_id = _required(package_id, "package_id")
    selector = _required(selector, "package selector").casefold()
    if selector != DEFAULT_ALIAS:
        try:
            version = int(selector)
        except ValueError as exc:
            raise PackageRegistryError("package selector 必须是 formal 或正整数版本") from exc
        if version < 1 or str(version) != selector:
            raise PackageRegistryError("package_version 必须是正整数")
        selector = str(version)
    return package_id, selector


def _package_files(root: Path) -> list[dict]:
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PackageRegistryError(f"不可变资产包不得包含符号链接: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise PackageRegistryError(f"资产包含非普通文件: {path}")
        relative = path.relative_to(root).as_posix()
        if relative.endswith((".sqlite3-shm", ".sqlite3-wal")):
            continue
        _safe_relative(relative)
        records.append({
            "path": relative,
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    if not records:
        raise PackageRegistryError("资产包为空")
    return records


def _package_stat_fingerprint(root: Path) -> tuple[tuple, ...]:
    """Cheaply detect package drift before reusing one fully verified record."""
    records = []
    pending = [(os.fspath(root), "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = prefix + entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise PackageRegistryError(f"不可变资产包不得包含符号链接: {entry.path}")
                if stat.S_ISDIR(info.st_mode):
                    pending.append((entry.path, relative + "/"))
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise PackageRegistryError(f"资产包含非普通文件: {entry.path}")
                if relative.endswith((".sqlite3-shm", ".sqlite3-wal")):
                    continue
                _safe_relative(relative)
                records.append((relative, info.st_dev, info.st_ino, info.st_size,
                                info.st_mtime_ns, info.st_ctime_ns))
    if not records:
        raise PackageRegistryError("资产包为空")
    return tuple(sorted(records))


def _verification_receipt(registry: Path, root: Path, record: dict, fingerprint: tuple) -> dict:
    return {
        "schema_version": _VERIFICATION_RECEIPT_SCHEMA,
        "registry": str(registry),
        "local_path": str(root.resolve()),
        "package_sha256": record["package_sha256"],
        "fingerprint": [list(row) for row in fingerprint],
    }


def _verification_cache_root(registry: Path, package_root: Path) -> Path | None:
    root = registry.parent / ".package-verification-cache"
    # Derived state must never become part of the immutable package it verifies.
    return None if root.is_relative_to(package_root.resolve()) else root


def _verification_key(root: Path, *, create: bool) -> bytes | None:
    """Keep receipts authenticated and disposable; cache failures never block reads."""
    if not hasattr(os, "getuid") or not hasattr(os, "O_NOFOLLOW"):
        return None
    try:
        if create:
            root.mkdir(mode=0o700, exist_ok=True)
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            return None
        path = root / ".key"
        if create:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(secrets.token_bytes(32))
                    handle.flush()
                    os.fsync(handle.fileno())
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size != 32):
                return None
            return handle.read(32)
    except OSError:
        return None


def _receipt_path(root: Path, receipt: dict) -> Path:
    identity = (receipt["registry"], receipt["local_path"], receipt["package_sha256"])
    return root / (sha256_json(identity) + ".json")


def _receipt_signature(key: bytes, receipt: dict) -> str:
    return hmac.new(key, sha256_json(receipt).encode("ascii"), hashlib.sha256).hexdigest()


def _read_verification_receipt(registry: Path, package_root: Path, expected: dict) -> bool:
    root = _verification_cache_root(registry, package_root)
    key = _verification_key(root, create=False) if root is not None else None
    if key is None:
        return False
    try:
        path = _receipt_path(root, expected)
        if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        return (isinstance(payload, dict) and payload.get("receipt") == expected
                and hmac.compare_digest(str(payload.get("signature") or ""),
                                        _receipt_signature(key, expected)))
    except (OSError, ValueError, TypeError):
        return False


def _write_verification_receipt(registry: Path, package_root: Path, receipt: dict) -> None:
    root = _verification_cache_root(registry, package_root)
    key = _verification_key(root, create=True) if root is not None else None
    if key is None:
        return
    try:
        write_json_atomic(_receipt_path(root, receipt), {
            "receipt": receipt, "signature": _receipt_signature(key, receipt),
        })
    except OSError:
        pass


def _verify_declared_integrity(package: dict, files: Iterable[dict]) -> None:
    by_path = {item["path"]: item for item in files}
    declared = (
        (str(package.get("index") or "asset_index.sqlite3"), package.get("index_sha256")),
        (str(package.get("blob_manifest") or ""), package.get("blob_manifest_sha256")),
        (str(package.get("template_rules") or ""), package.get("template_rules_sha256")),
    )
    for relative, expected in declared:
        if not relative or not expected:
            continue
        actual = by_path.get(_safe_relative(relative).as_posix())
        if not actual or actual["sha256"] != expected:
            raise PackageRegistryError(f"资产包声明的文件 SHA-256 不一致: {relative}")
    blob_manifest = package.get("blob_manifest")
    if blob_manifest:
        path = Path(package["package_root"]) / _safe_relative(blob_manifest)
        try:
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise PackageRegistryError("资产包 blob manifest 无法读取") from exc
        for row in rows:
            relative = _safe_relative(row.get("path")).as_posix()
            actual = by_path.get(relative)
            if not actual or actual["sha256"] != row.get("content_sha256"):
                raise PackageRegistryError(f"资产包媒体 SHA-256 不一致: {relative}")


def _package_key(package_id: str, package_version: int) -> str:
    normalized_id = _required(package_id, "package_id")
    if type(package_version) is not int or package_version < 1:
        raise PackageRegistryError("package_version 必须是正整数")
    return f"{normalized_id}@{package_version}"


def _alias(value: str) -> str:
    normalized = _required(value, "alias").casefold()
    if normalized != DEFAULT_ALIAS:
        raise PackageRegistryError("当前只允许 formal 作为可移动别名")
    return normalized


def _required(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise PackageRegistryError(f"{field} 不得为空")
    return normalized


def _safe_relative(value: object) -> PurePosixPath:
    text = str(value or "")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PackageRegistryError("资产包包含不安全路径")
    return path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
