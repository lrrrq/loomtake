from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .workspace_store import WorkspaceServiceError


_PORTFOLIO_REF = re.compile(r"^artifact://creative-strategy/([0-9a-f]{64})$")


class CreativeStrategyArtifactStore:
    """Content-addressed storage for evidence-bound strategy portfolios."""

    def __init__(self, data_root: str | Path):
        self.root = (
            Path(data_root).expanduser().resolve()
            / "artifacts"
            / "creative-strategies"
        )

    def freeze(self, portfolio: dict, *, bindings: dict) -> dict:
        if not isinstance(portfolio, dict) or not portfolio:
            raise WorkspaceServiceError("INVALID_INPUT", "策略 portfolio 必须是非空 object")
        if not isinstance(bindings, dict) or not bindings:
            raise WorkspaceServiceError("INVALID_INPUT", "策略 bindings 必须是非空 object")
        payload = {
            "schema_version": "mplus-creative-strategy-artifact/v1",
            "bindings": bindings,
            "portfolio": portfolio,
        }
        encoded = _canonical_json(payload).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        target = self.root / digest[:2] / f"{digest}.json"
        try:
            if target.is_file():
                if _sha256_file(target) != digest:
                    raise WorkspaceServiceError(
                        "ARTIFACT_HASH_MISMATCH",
                        "已有策略 artifact 内容与地址不一致",
                    )
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_once(target, encoded)
        except OSError as exc:
            raise WorkspaceServiceError(
                "ARTIFACT_WRITE_FAILED", "策略 portfolio artifact 无法落盘"
            ) from exc
        return {
            "portfolio_ref": f"artifact://creative-strategy/{digest}",
            "artifact_sha256": digest,
            "size_bytes": len(encoded),
        }

    def read(self, portfolio_ref: str) -> dict:
        match = _PORTFOLIO_REF.fullmatch(str(portfolio_ref or "").strip())
        if match is None:
            raise WorkspaceServiceError(
                "INVALID_INPUT", "strategy_portfolio_ref 不是受控策略 artifact"
            )
        digest = match.group(1)
        path = self.root / digest[:2] / f"{digest}.json"
        try:
            if not path.is_file():
                raise WorkspaceServiceError("NOT_FOUND", "策略 portfolio artifact 不存在")
            if _sha256_file(path) != digest:
                raise WorkspaceServiceError(
                    "ARTIFACT_HASH_MISMATCH", "策略 portfolio artifact SHA-256 已变化"
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise WorkspaceServiceError(
                "ARTIFACT_READ_FAILED", "策略 portfolio artifact 无法读取"
            ) from exc
        except json.JSONDecodeError as exc:
            raise WorkspaceServiceError(
                "ARTIFACT_HASH_MISMATCH", "策略 portfolio artifact 无法解析"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version")
            != "mplus-creative-strategy-artifact/v1"
            or not isinstance(payload.get("bindings"), dict)
            or not isinstance(payload.get("portfolio"), dict)
        ):
            raise WorkspaceServiceError(
                "ARTIFACT_HASH_MISMATCH", "策略 portfolio artifact 合同无效"
            )
        return {
            **payload,
            "portfolio_ref": str(portfolio_ref),
            "artifact_sha256": digest,
        }


def _write_once(path: Path, value: bytes) -> None:
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        os.fchmod(descriptor, 0o640)
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


def _canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
