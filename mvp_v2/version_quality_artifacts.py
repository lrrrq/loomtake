from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Protocol

from .workspace_store import WorkspaceServiceError


_KINDS = {"shot-review", "final-confirmation"}
_REF = re.compile(
    r"^artifact://version-quality/(shot-review|final-confirmation)/([0-9a-f]{64})$"
)


@dataclass(frozen=True)
class VersionQualityArtifact:
    ref: str
    sha256: str


class VersionQualityArtifactWriter(Protocol):
    def write_json(self, kind: str, payload: dict) -> VersionQualityArtifact: ...

    def read_json(self, ref: str, sha256: str) -> dict: ...


class FileVersionQualityArtifactStore:
    """Content-addressed JSON evidence store; links persist through migration-9 adapter.

    The file is immutable by SHA-address and may be reopened by a new resolver.
    It is an injected runtime dependency, never a workspace migration or source
    artifact to commit.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve() / "artifacts" / "version-quality"

    def write_json(self, kind: str, payload: dict) -> VersionQualityArtifact:
        if kind not in _KINDS or not isinstance(payload, dict):
            raise WorkspaceServiceError("INVALID_INPUT", "quality artifact 类型或 JSON 无效")
        encoded = _canonical_json(payload).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        target = self.root / kind / digest[:2] / f"{digest}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _sha256_file(target) != digest:
                raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "已有 quality artifact 内容冲突")
        else:
            _write_once(target, encoded)
        return VersionQualityArtifact(
            ref=f"artifact://version-quality/{kind}/{digest}", sha256=digest
        )

    def read_json(self, ref: str, sha256: str) -> dict:
        match = _REF.fullmatch(str(ref or "").strip())
        expected = str(sha256 or "").strip().lower()
        if match is None or match.group(2) != expected:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "quality artifact 引用与 SHA 不一致")
        kind, digest = match.groups()
        path = self.root / kind / digest[:2] / f"{digest}.json"
        if not path.is_file():
            raise WorkspaceServiceError("NOT_FOUND", "quality artifact 不存在")
        if _sha256_file(path) != digest:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "quality artifact 字节已变化")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "quality artifact 不是可读 JSON") from exc
        if not isinstance(payload, dict):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "quality artifact 根必须是 object")
        return payload


def _canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_once(path: Path, value: bytes) -> None:
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            pass
    finally:
        temp.unlink(missing_ok=True)
