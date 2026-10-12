"""Server-owned immutable evidence for manual version review commands."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .workspace_store import WorkspaceServiceError, utc_now


_KINDS = {"review-item", "human-decision", "focus-recheck", "final-confirmation"}
_REF = re.compile(r"^artifact://version-review-evidence/([0-9a-f]{64})$")
_SCHEMA = "mplus-version-review-evidence/v1"


@dataclass(frozen=True)
class ReviewEvidenceArtifact:
    ref: str
    sha256: str
    occurred_at: str


class FileReviewEvidenceArtifactStore:
    """Content-addressed review evidence store, injected outside employee input."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve() / "artifacts" / "version-review-evidence"

    def write_json(self, payload: dict) -> ReviewEvidenceArtifact:
        if not isinstance(payload, dict):
            raise WorkspaceServiceError("INVALID_INPUT", "审核证据必须是 JSON object")
        encoded = _canonical(payload).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        target = self.root / digest[:2] / f"{digest}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _sha_file(target) != digest:
                raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "已有审核证据内容冲突")
        else:
            _write_once(target, encoded)
        return ReviewEvidenceArtifact(
            f"artifact://version-review-evidence/{digest}", digest, str(payload.get("occurred_at") or "")
        )

    def read_json(self, ref: str, sha256: str) -> dict:
        match = _REF.fullmatch(str(ref or "").strip())
        digest = str(sha256 or "").strip().lower()
        if match is None or match.group(1) != digest:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "审核证据引用与 SHA 不一致")
        path = self.root / digest[:2] / f"{digest}.json"
        if not path.is_file():
            raise WorkspaceServiceError("NOT_FOUND", "审核证据不存在")
        if _sha_file(path) != digest:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "审核证据字节已变化")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "审核证据不是可读 JSON") from exc
        if not isinstance(payload, dict):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "审核证据根必须是 object")
        return payload


class ReviewEvidenceAuthority:
    """Only this authority turns a manual note/decision into persisted evidence."""

    def __init__(self, artifacts: FileReviewEvidenceArtifactStore):
        self.artifacts = artifacts

    def materialize(
        self, context: dict, *, kind: str, actor_id: str, note: str,
        review_item_id: str | None = None, decision: str | None = None,
        occurred_at: str | None = None, snapshot_ref: str | None = None,
        snapshot_sha256: str | None = None, confirmation_id: str | None = None,
    ) -> ReviewEvidenceArtifact:
        if kind not in _KINDS or not actor_id or not isinstance(note, str):
            raise WorkspaceServiceError("INVALID_INPUT", "审核证据业务字段无效")
        payload = {
            "schema_version": _SCHEMA,
            "workspace_id": context["workspace_id"], "project_id": context["project_id"],
            "version_id": context["version_id"], "run_id": context["run_id"],
            "kind": kind, "actor_id": actor_id, "occurred_at": occurred_at or utc_now(),
            "note": note,
            "review_item_id": review_item_id, "decision": decision,
            "snapshot_ref": snapshot_ref, "snapshot_sha256": snapshot_sha256,
            "confirmation_id": confirmation_id,
        }
        artifact = self.artifacts.write_json(payload)
        self.verify(
            context, artifact, kind=kind, actor_id=actor_id, note=note,
            review_item_id=review_item_id, decision=decision,
            occurred_at=payload["occurred_at"], snapshot_ref=snapshot_ref,
            snapshot_sha256=snapshot_sha256, confirmation_id=confirmation_id,
        )
        return artifact

    def verify(
        self, context: dict, artifact: ReviewEvidenceArtifact, *, kind: str,
        actor_id: str, note: str, review_item_id: str | None = None,
        decision: str | None = None, occurred_at: str | None = None,
        snapshot_ref: str | None = None, snapshot_sha256: str | None = None,
        confirmation_id: str | None = None,
    ) -> dict:
        payload = self.artifacts.read_json(artifact.ref, artifact.sha256)
        expected = {
            "schema_version": _SCHEMA,
            "workspace_id": context["workspace_id"], "project_id": context["project_id"],
            "version_id": context["version_id"], "run_id": context["run_id"],
            "kind": kind, "actor_id": actor_id, "note": note,
            "review_item_id": review_item_id, "decision": decision,
            "snapshot_ref": snapshot_ref, "snapshot_sha256": snapshot_sha256,
            "confirmation_id": confirmation_id,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "审核证据与当前项目、版本、运行或人工决定不一致")
        if occurred_at is not None and payload.get("occurred_at") != occurred_at:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "审核证据时间不一致")
        if not isinstance(payload.get("occurred_at"), str) or not payload["occurred_at"]:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "审核证据缺少服务端时间")
        return payload

    def verify_existing(
        self, context: dict, *, ref: str, sha256: str, kind: str,
        review_item_id: str | None = None, decision: str | None = None,
        actor_id: str | None = None, occurred_at: str | None = None,
    ) -> dict:
        payload = self.artifacts.read_json(ref, sha256)
        expected = {
            "schema_version": _SCHEMA,
            "workspace_id": context["workspace_id"], "project_id": context["project_id"],
            "version_id": context["version_id"], "run_id": context["run_id"],
            "kind": kind, "review_item_id": review_item_id, "decision": decision,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "已登记审核证据与当前上下文或审核记录不一致")
        if actor_id is not None and payload.get("actor_id") != actor_id:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "已登记审核证据审核人不一致")
        if occurred_at is not None and payload.get("occurred_at") != occurred_at:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "已登记审核证据时间不一致")
        if not isinstance(payload.get("actor_id"), str) or not isinstance(payload.get("occurred_at"), str):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "已登记审核证据缺少审核人或时间")
        source = payload.get("inherited_from")
        if source is not None:
            if (not isinstance(source, dict) or kind != "human-decision" or decision != "approve"
                    or source.get("workspace_id") != context["workspace_id"]
                    or source.get("project_id") != context["project_id"]
                    or source.get("version_id") == context["version_id"]):
                raise WorkspaceServiceError("ARTIFACT_INVALID", "继承审核来源绑定不一致")
            self.verify_existing(source, ref=source["evidence_ref"], sha256=source["evidence_sha256"],
                kind="human-decision", review_item_id=source["review_item_id"], decision="approve",
                actor_id=payload["actor_id"], occurred_at=payload["occurred_at"])
            self.verify_existing(source, ref=source["item_evidence_ref"], sha256=source["item_evidence_sha256"],
                kind="review-item", review_item_id=source["review_item_id"], occurred_at=source["item_created_at"])
        return payload

    def materialize_inherited(self, context: dict, *, review_item_id: str, source: dict) -> ReviewEvidenceArtifact:
        original = self.verify_existing(source, ref=source["evidence_ref"], sha256=source["evidence_sha256"],
            kind="human-decision", review_item_id=source["review_item_id"], decision="approve",
            actor_id=source["reviewer_actor_id"], occurred_at=source["decided_at"])
        payload = {**original, **{key: context[key] for key in ("workspace_id", "project_id", "version_id", "run_id")},
            "review_item_id": review_item_id, "inherited_from": dict(source)}
        artifact = self.artifacts.write_json(payload)
        self.verify_existing(context, ref=artifact.ref, sha256=artifact.sha256, kind="human-decision",
            review_item_id=review_item_id, decision="approve", actor_id=source["reviewer_actor_id"], occurred_at=source["decided_at"])
        return artifact


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_file(path: Path) -> str:
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
