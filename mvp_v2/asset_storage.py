from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
from typing import BinaryIO
import uuid


STORAGE_REF_PREFIX = "sha256://"


class AssetStorageError(ValueError):
    pass


class LocalAssetStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def put(self, stream: BinaryIO) -> tuple[str, str]:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".put-{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        try:
            with temporary.open("xb") as handle:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            sha256 = digest.hexdigest()
            target = self._object_path(sha256)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if self._sha256(target) != sha256:
                    raise AssetStorageError("STORAGE_INTEGRITY_ERROR")
                temporary.unlink()
            else:
                os.replace(temporary, target)
            return f"{STORAGE_REF_PREFIX}{sha256}", sha256
        finally:
            if temporary.exists():
                temporary.unlink()

    def open(self, storage_ref: str) -> BinaryIO:
        digest = self._digest(storage_ref)
        path = self._object_path(digest)
        if not path.is_file():
            raise AssetStorageError("STORAGE_NOT_FOUND")
        if self._sha256(path) != digest:
            raise AssetStorageError("STORAGE_INTEGRITY_ERROR")
        return path.open("rb")

    def exists(self, storage_ref: str) -> bool:
        try:
            digest = self._digest(storage_ref)
        except AssetStorageError:
            return False
        return self._object_path(digest).is_file()

    def materialize(self, storage_ref: str, target: str | Path) -> Path:
        digest = self._digest(storage_ref)
        destination = Path(target).expanduser()
        if destination.exists():
            if destination.is_file() and self._sha256(destination) == digest:
                return destination
            raise AssetStorageError("STORAGE_TARGET_CONFLICT")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
        try:
            with self.open(storage_ref) as source, temporary.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            return destination
        finally:
            if temporary.exists():
                temporary.unlink()

    def verify(self, storage_ref: str, sha256: str) -> bool:
        digest = self._digest(storage_ref)
        if digest != str(sha256).strip().casefold():
            raise AssetStorageError("STORAGE_INTEGRITY_ERROR")
        path = self._object_path(digest)
        if not path.is_file():
            raise AssetStorageError("STORAGE_NOT_FOUND")
        if self._sha256(path) != digest:
            raise AssetStorageError("STORAGE_INTEGRITY_ERROR")
        return True

    def path_for(self, storage_ref: str) -> Path:
        return self._object_path(self._digest(storage_ref))

    def _object_path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    @staticmethod
    def _digest(storage_ref: str) -> str:
        value = str(storage_ref or "").strip().casefold()
        if not value.startswith(STORAGE_REF_PREFIX):
            raise AssetStorageError("STORAGE_REF_INVALID")
        digest = value[len(STORAGE_REF_PREFIX):]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise AssetStorageError("STORAGE_REF_INVALID")
        return digest

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
