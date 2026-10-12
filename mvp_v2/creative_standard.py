"""Immutable Creative Standard Card contracts paired to confirmed formal Briefs."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Protocol

from .formal_brief_domain import ArtifactPayload, ConfirmedBriefInput, PendingBriefVersion
from .workspace_store import WorkspaceServiceError


CREATIVE_STANDARD_SCHEMA = "mplus-creative-standard-card/v1"
CONFIRMED_CREATIVE_STANDARD_SCHEMA = "mplus-confirmed-creative-standard-input/v1"
_UTC_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$")
_REQUIRED = ("tone_keywords", "expression_focus")
_FIELD_GROUPS = {
    "tone_keywords": ("风格调性",),
    "expression_focus": ("视频目标", "必须出现的内容", "关键事件与流程"),
    "avoid_expressions": ("禁止或不能出现的内容", "禁忌或不能出现的内容"),
    "references": ("参考方案", "参考图片", "历史脚本"),
}


class CreativeStandardError(ValueError):
    pass


@dataclass(frozen=True)
class CreativeStandardVersion:
    workspace_id: str
    project_id: str
    creative_standard_version_id: str
    brief_version_id: str
    brief_payload: ArtifactPayload
    payload: ArtifactPayload
    content_sha256: str
    content: dict
    created_by_actor_id: str
    created_at: str


@dataclass(frozen=True)
class ConfirmedCreativeStandardInput:
    schema_version: str
    workspace_id: str
    project_id: str
    creative_standard_version_id: str
    brief_version_id: str
    confirmation_id: str
    brief_snapshot_id: str
    brief_payload: ArtifactPayload
    card_payload: ArtifactPayload
    confirmation_status: str
    confirmed_by_actor_id: str
    confirmed_at: str

    def __post_init__(self) -> None:
        if (
            not all(str(value or "").strip() for value in (
                self.workspace_id, self.project_id, self.creative_standard_version_id,
                self.brief_version_id, self.confirmation_id, self.brief_snapshot_id,
                self.confirmed_by_actor_id,
            ))
            or self.schema_version != CONFIRMED_CREATIVE_STANDARD_SCHEMA or self.confirmation_status != "confirmed"
            or not _UTC_Z.fullmatch(self.confirmed_at)
        ):
            raise CreativeStandardError("ConfirmedCreativeStandardInput 合同无效")
        if not isinstance(self.brief_payload, ArtifactPayload) or not isinstance(self.card_payload, ArtifactPayload):
            raise TypeError("ConfirmedCreativeStandardInput 只接受 ArtifactPayload")

    def as_mapping(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "creative_standard_version_id": self.creative_standard_version_id,
            "brief_version_id": self.brief_version_id,
            "confirmation_id": self.confirmation_id,
            "brief_snapshot_id": self.brief_snapshot_id,
            "brief_payload": self.brief_payload.as_mapping(),
            "card_payload": self.card_payload.as_mapping(),
            "confirmation_status": self.confirmation_status,
            "confirmed_by_actor_id": self.confirmed_by_actor_id,
            "confirmed_at": self.confirmed_at,
        }


@dataclass(frozen=True)
class PairedConfirmedBriefCreativeStandardInput:
    brief: ConfirmedBriefInput
    creative_standard: ConfirmedCreativeStandardInput

    def __post_init__(self) -> None:
        if not isinstance(self.brief, ConfirmedBriefInput) or not isinstance(self.creative_standard, ConfirmedCreativeStandardInput):
            raise TypeError("paired input 只接受确认后的强类型输入")
        if (
            self.brief.workspace_id, self.brief.project_id, self.brief.brief_version_id,
            self.brief.brief_snapshot_id, self.brief.payload,
        ) != (
            self.creative_standard.workspace_id, self.creative_standard.project_id,
            self.creative_standard.brief_version_id,
            self.creative_standard.brief_snapshot_id, self.creative_standard.brief_payload,
        ):
            raise CreativeStandardError("confirmed Brief 与标准卡绑定不一致")


class CreativeStandardArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve() / "creative-standard-payloads"
        self._created: ContextVar[tuple[Path, ...]] = ContextVar("creative_standard_created_artifacts", default=())

    def checkpoint(self) -> int:
        return len(self._created.get())

    def rollback_to(self, checkpoint: int) -> None:
        created = self._created.get()
        # Published content may already be referenced by another committed request.
        # Request rollback only forgets its bookkeeping; shared blobs are immutable.
        self._created.set(created[:checkpoint])

    def write(self, payload: bytes) -> ArtifactPayload:
        digest = sha256(payload).hexdigest()
        target = self.root / digest[:2] / f"{digest}.json"
        if target.is_file():
            if sha256(target.read_bytes()).hexdigest() != digest:
                raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "标准卡 artifact 内容已损坏")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if _write_once(target, payload):
                self._created.set((*self._created.get(), target))
            elif sha256(target.read_bytes()).hexdigest() != digest:
                raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "标准卡 artifact 内容已损坏")
        return ArtifactPayload(f"artifact://creative-standard/{digest}", digest)

    def read(self, payload: ArtifactPayload) -> bytes:
        if not isinstance(payload, ArtifactPayload) or payload.ref != f"artifact://creative-standard/{payload.sha256}":
            raise WorkspaceServiceError("CREATIVE_STANDARD_UNAVAILABLE", "标准卡 artifact 引用无效")
        target = self.root / payload.sha256[:2] / f"{payload.sha256}.json"
        if not target.is_file():
            raise WorkspaceServiceError("CREATIVE_STANDARD_UNAVAILABLE", "标准卡 artifact 不存在")
        raw = target.read_bytes()
        if sha256(raw).hexdigest() != payload.sha256:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "标准卡 artifact SHA-256 已变化")
        return raw


class CreativeStandardDomainService:
    def __init__(self, artifacts: CreativeStandardArtifactStore, id_factory):
        self.artifacts, self.id_factory = artifacts, id_factory

    def draft(self, brief: PendingBriefVersion, *, actor_id: str, created_at: str) -> CreativeStandardVersion:
        if not isinstance(brief, PendingBriefVersion):
            raise TypeError("标准卡草稿只接受 PendingBriefVersion")
        content, reference_projection = _project_content(brief.payload_data)
        content_sha256 = sha256(_canonical(content).encode("utf-8")).hexdigest()
        standard_id = self.id_factory("creative_standard")
        envelope = {
            "schema_version": CREATIVE_STANDARD_SCHEMA,
            "workspace_id": brief.workspace_id,
            "project_id": brief.project_id,
            "creative_standard_version_id": standard_id,
            "brief_version_id": brief.brief_version_id,
            "brief_payload": brief.payload.as_mapping(),
            "content_sha256": content_sha256,
            "content": content,
            "reference_projection": reference_projection,
        }
        payload = self.artifacts.write(_canonical(envelope).encode("utf-8"))
        return CreativeStandardVersion(
            brief.workspace_id, brief.project_id, standard_id, brief.brief_version_id,
            brief.payload, payload, content_sha256, content, actor_id, created_at,
        )

    def read(self, version: CreativeStandardVersion) -> dict:
        if not isinstance(version, CreativeStandardVersion):
            raise TypeError("标准卡读取只接受 CreativeStandardVersion")
        raw = self.artifacts.read(version.payload)
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("CREATIVE_STANDARD_UNAVAILABLE", "标准卡 artifact 无法解析") from exc
        expected = {
            "schema_version": CREATIVE_STANDARD_SCHEMA,
            "workspace_id": version.workspace_id,
            "project_id": version.project_id,
            "creative_standard_version_id": version.creative_standard_version_id,
            "brief_version_id": version.brief_version_id,
            "brief_payload": version.brief_payload.as_mapping(),
            "content_sha256": version.content_sha256,
        }
        if not isinstance(envelope, dict) or any(envelope.get(key) != value for key, value in expected.items()):
            raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", "标准卡 artifact 与同库版本不一致")
        content = envelope.get("content")
        if not isinstance(content, dict) or sha256(_canonical(content).encode("utf-8")).hexdigest() != version.content_sha256:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "标准卡内容 SHA-256 已变化")
        _validate_content(content, require_complete=False)
        return content

    def require_confirmable(self, version: CreativeStandardVersion) -> dict:
        content = self.read(version)
        gaps = self.confirmation_gaps(version)
        if gaps:
            raise WorkspaceServiceError(
                "CREATIVE_STANDARD_NEEDS_CLARIFICATION",
                f"标准卡缺少已确认的 {gaps[0]}",
            )
        return content

    def confirmation_gaps(
        self, version: CreativeStandardVersion, *, content: dict | None = None,
    ) -> tuple[str, ...]:
        """Return business-visible missing required sections without inventing data."""
        current = self.read(version) if content is None else content
        _validate_content(current, require_complete=False)
        gaps = [name for name in _REQUIRED if not current[name]]
        if self._reference_projection_has_gaps(version):
            gaps.append("references")
        return tuple(gaps)

    def _reference_projection_has_gaps(self, version: CreativeStandardVersion) -> bool:
        raw = self.artifacts.read(version.payload)
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("CREATIVE_STANDARD_UNAVAILABLE", "标准卡 artifact 无法解析") from exc
        projection = envelope.get("reference_projection", {}) if isinstance(envelope, dict) else {}
        missing = projection.get("missing_fact_ids", []) if isinstance(projection, dict) else []
        if not isinstance(missing, list) or not all(isinstance(item, str) and item for item in missing):
            raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", "标准卡参考资料投影无效")
        return bool(missing)


class CreativeStandardAuthority(Protocol):
    def load_confirmed_creative_standard(self, value: ConfirmedCreativeStandardInput) -> ConfirmedCreativeStandardInput: ...
    def read_creative_standard_bytes(self, payload: ArtifactPayload) -> bytes: ...


class ConfirmedCreativeStandardResolver:
    def __init__(self, authority: CreativeStandardAuthority):
        self.authority = authority

    def resolve(self, value: ConfirmedCreativeStandardInput) -> dict:
        if not isinstance(value, ConfirmedCreativeStandardInput):
            raise TypeError("Resolver 只接受 ConfirmedCreativeStandardInput，拒绝 Mapping")
        stored = self.authority.load_confirmed_creative_standard(value)
        if stored != value:
            raise CreativeStandardError("ConfirmedCreativeStandardInput 与同库确认记录不一致")
        raw = self.authority.read_creative_standard_bytes(value.card_payload)
        if sha256(raw).hexdigest() != value.card_payload.sha256:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "标准卡 payload SHA-256 已变化")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("CREATIVE_STANDARD_UNAVAILABLE", "标准卡 payload 无法解析") from exc
        if not isinstance(envelope, dict) or any(envelope.get(key) != getattr(value, key) for key in ("workspace_id", "project_id", "creative_standard_version_id", "brief_version_id")):
            raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", "标准卡 payload 与确认绑定不一致")
        if envelope.get("brief_payload") != value.brief_payload.as_mapping():
            raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", "标准卡 Brief payload 绑定已变化")
        content = envelope.get("content")
        if not isinstance(content, dict) or sha256(_canonical(content).encode("utf-8")).hexdigest() != envelope.get("content_sha256"):
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "标准卡内容 SHA-256 已变化")
        projection = envelope.get("reference_projection", {})
        if not isinstance(projection, dict) or not isinstance(projection.get("missing_fact_ids", []), list):
            raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", "标准卡参考资料投影无效")
        if projection.get("missing_fact_ids"):
            raise WorkspaceServiceError("CREATIVE_STANDARD_NEEDS_CLARIFICATION", "标准卡参考资料缺少已确认事实或证据")
        _validate_content(content, require_complete=True)
        return content


def _project_content(payload: dict) -> tuple[dict, dict]:
    facts = payload.get("facts") if isinstance(payload, dict) else None
    conflicts = payload.get("conflicts") if isinstance(payload, dict) else None
    if not isinstance(facts, list) or not isinstance(conflicts, list):
        raise CreativeStandardError("正式 Brief facts/conflicts 合同缺失")
    blocked_fact_ids = {
        fact_id for conflict in conflicts if isinstance(conflict, dict)
        and conflict.get("resolution_status") == "unresolved"
        for fact_id in conflict.get("fact_ids", [])
    }
    resolved_by_field = {
        str(conflict.get("field_path") or ""): str(conflict.get("resolved_fact_id") or "")
        for conflict in conflicts if isinstance(conflict, dict) and conflict.get("resolution_status") == "resolved"
    }
    result = {key: [] for key in _FIELD_GROUPS}
    references = {"source_fact_ids": [], "projected_fact_ids": [], "missing_fact_ids": []}
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fact_id, field = str(fact.get("fact_id") or ""), str(fact.get("field_path") or "")
        is_reference = field in _FIELD_GROUPS["references"]
        if is_reference and fact_id:
            references["source_fact_ids"].append(fact_id)
        if fact.get("certainty") != "confirmed" or not fact_id or fact_id in blocked_fact_ids:
            if is_reference and fact_id:
                references["missing_fact_ids"].append(fact_id)
            continue
        selected = resolved_by_field.get(field)
        if selected and selected != fact_id:
            continue
        refs = fact.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            if is_reference:
                references["missing_fact_ids"].append(fact_id)
            continue
        try:
            value = json.loads(str(fact.get("value_json") or ""))
        except json.JSONDecodeError:
            if is_reference:
                references["missing_fact_ids"].append(fact_id)
            continue
        if not isinstance(value, str) or not value.strip():
            if is_reference:
                references["missing_fact_ids"].append(fact_id)
            continue
        entry = {"value": value.strip(), "fact_id": fact_id, "evidence_refs": refs}
        for group, fields in _FIELD_GROUPS.items():
            if field in fields and entry not in result[group]:
                result[group].append(entry)
                if group == "references":
                    references["projected_fact_ids"].append(fact_id)
    references = {key: sorted(set(value)) for key, value in references.items()}
    return result, references


def _validate_content(content: dict, *, require_complete: bool) -> None:
    if not isinstance(content, dict) or set(content) != set(_FIELD_GROUPS):
        raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", "标准卡字段不完整")
    for name, entries in content.items():
        if not isinstance(entries, list):
            raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", f"标准卡 {name} 必须是 array")
        if require_complete and name in _REQUIRED and not entries:
            raise WorkspaceServiceError("CREATIVE_STANDARD_NEEDS_CLARIFICATION", f"标准卡缺少已确认的 {name}")
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"value", "fact_id", "evidence_refs"}:
                raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", f"标准卡 {name} 条目合同无效")
            if not isinstance(entry["value"], str) or not entry["value"].strip() or not isinstance(entry["fact_id"], str) or not entry["fact_id"] or not isinstance(entry["evidence_refs"], list) or not entry["evidence_refs"]:
                raise WorkspaceServiceError("CREATIVE_STANDARD_NEEDS_CLARIFICATION", f"标准卡 {name} 缺少事实或证据")
            if entry["fact_id"] in seen:
                raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", f"标准卡 {name} 存在重复事实")
            seen.add(entry["fact_id"])


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_once(path: Path, payload: bytes) -> bool:
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temp, path)
            return True
        except FileExistsError:
            return False
    finally:
        temp.unlink(missing_ok=True)
