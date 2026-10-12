from __future__ import annotations

from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile

from .workspace_store import WorkspaceServiceError


_REF_PATTERN = re.compile(r"^artifact://brief/([0-9a-f]{64})$")


class BriefArtifactStore:
    """Content-addressed, local storage for frozen normalized Briefs."""

    def __init__(self, data_root: str | Path):
        self.root = Path(data_root).expanduser().resolve() / "artifacts" / "briefs"
        self._created: ContextVar[tuple[Path, ...]] = ContextVar(
            "brief_created_artifacts", default=(),
        )

    def checkpoint(self) -> int:
        return len(self._created.get())

    def rollback_to(self, checkpoint: int) -> None:
        created = self._created.get()
        # Published content may already be referenced by another committed request.
        # Request rollback only forgets its bookkeeping; shared blobs are immutable.
        self._created.set(created[:checkpoint])

    def freeze(self, brief: dict) -> dict:
        if not isinstance(brief, dict) or not brief:
            raise WorkspaceServiceError("INVALID_INPUT", "Brief 必须是非空 object")
        payload = {
            "schema_version": "mplus-brief-snapshot/v1",
            "brief": brief,
        }
        encoded = _canonical_json(payload).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        target = self.root / digest[:2] / f"{digest}.json"
        if target.is_file():
            if _sha256_file(target) != digest:
                raise WorkspaceServiceError(
                    "ARTIFACT_HASH_MISMATCH", "已有 Brief artifact 内容与地址不一致"
                )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if _write_once(target, encoded):
                self._created.set((*self._created.get(), target))
            elif _sha256_file(target) != digest:
                raise WorkspaceServiceError(
                    "ARTIFACT_HASH_MISMATCH", "已有 Brief artifact 内容与地址不一致"
                )
        return {
            "brief_ref": f"artifact://brief/{digest}",
            "brief_sha256": digest,
            "size_bytes": len(encoded),
        }

    def read(self, brief_ref: str, expected_sha256: str) -> dict:
        match = _REF_PATTERN.fullmatch(str(brief_ref or "").strip())
        expected = str(expected_sha256 or "").strip().lower()
        if match is None or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise WorkspaceServiceError(
                "BRIEF_ARTIFACT_UNAVAILABLE", "Brief artifact 引用或 SHA-256 无效"
            )
        digest = match.group(1)
        if digest != expected:
            raise WorkspaceServiceError(
                "ARTIFACT_HASH_MISMATCH", "Brief artifact 引用与登记 SHA-256 不一致"
            )
        path = self.root / digest[:2] / f"{digest}.json"
        if not path.is_file():
            raise WorkspaceServiceError(
                "BRIEF_ARTIFACT_UNAVAILABLE", "已登记的 Brief artifact 不存在"
            )
        if _sha256_file(path) != digest:
            raise WorkspaceServiceError(
                "ARTIFACT_HASH_MISMATCH", "Brief artifact SHA-256 已变化"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError(
                "BRIEF_ARTIFACT_UNAVAILABLE", "Brief artifact 无法读取"
            ) from exc
        brief = payload.get("brief") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != "mplus-brief-snapshot/v1"
            or not isinstance(brief, dict)
        ):
            raise WorkspaceServiceError(
                "BRIEF_ARTIFACT_UNAVAILABLE", "Brief artifact 合同无效"
            )
        return dict(brief)


def load_or_create_confirmation_key(data_root: str | Path) -> bytes:
    path = Path(data_root).expanduser().resolve() / "secrets" / "agent-confirmation.key"
    if path.is_file():
        value = path.read_bytes()
        if len(value) != 32:
            raise WorkspaceServiceError(
                "INTEGRITY_ERROR", "Agent 确认签名密钥长度无效"
            )
        return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(32)
    _write_once(path, value, mode=0o600)
    stored = path.read_bytes()
    if len(stored) != 32:
        raise WorkspaceServiceError("INTEGRITY_ERROR", "Agent 确认签名密钥写入失败")
    return stored


def _write_once(path: Path, value: bytes, *, mode: int = 0o640) -> bool:
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
            return True
        except FileExistsError:
            return False
    finally:
        temp.unlink(missing_ok=True)


def _canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
