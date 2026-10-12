"""Database-neutral T01 Brief contracts for later same-transaction integration."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from pathlib import Path
from typing import Callable, Protocol


FORMAL_BRIEF_SCHEMA = "mplus-formal-brief/v1"
_CONFIRMED_INPUT_FIELDS = frozenset({
    "workspace_id", "project_id", "brief_version_id", "confirmation_id",
    "brief_snapshot_id", "payload", "confirmation_status",
    "unresolved_conflict_count", "confirmed_by_actor_id", "confirmed_at",
})
_UTC_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$")


class FormalBriefDomainError(ValueError):
    """Visible contract failure; the authoritative adapter maps it to service errors."""


@dataclass(frozen=True)
class FormalBriefOcrPolicy:
    """Explicit document transcription through the existing vision provider."""

    invoke: Callable[[Path, str], str]
    provider: str
    model: str
    pdftoppm_command: str | None = None
    timeout_seconds: int = 90


@dataclass(frozen=True)
class ArtifactPayload:
    ref: str
    sha256: str

    @classmethod
    def from_mapping(cls, value: dict) -> "ArtifactPayload":
        if not isinstance(value, dict) or set(value) != {"ref", "sha256"}:
            raise FormalBriefDomainError("ArtifactRef/SHA 必须成对且仅含 ref、sha256")
        ref, digest = str(value["ref"] or "").strip(), str(value["sha256"] or "").strip().lower()
        if not ref.startswith("artifact://") or not _is_sha256(digest):
            raise FormalBriefDomainError("ArtifactRef 或 SHA-256 无效")
        return cls(ref, digest)

    def as_mapping(self) -> dict:
        return {"ref": self.ref, "sha256": self.sha256}


@dataclass(frozen=True)
class FrozenDocument:
    source_document_id: str
    relative_path: str
    content_sha256: str
    critical: bool

    @classmethod
    def from_mapping(cls, value: dict) -> "FrozenDocument":
        required = {"source_document_id", "relative_path", "content_sha256", "critical"}
        if not isinstance(value, dict) or set(value) != required or type(value["critical"]) is not bool:
            raise FormalBriefDomainError("FrozenDocument 合同无效")
        document_id, path, digest = (str(value[key] or "").strip() for key in ("source_document_id", "relative_path", "content_sha256"))
        if not document_id or not path or path.startswith("/") or ".." in path.split("/") or not _is_sha256(digest.lower()):
            raise FormalBriefDomainError("冻结文件标识、相对路径或 SHA-256 无效")
        return cls(document_id, path, digest.lower(), value["critical"])


@dataclass(frozen=True)
class SourceManifest:
    manifest: ArtifactPayload
    documents: tuple[FrozenDocument, ...]

    @classmethod
    def from_mapping(cls, value: dict) -> "SourceManifest":
        if not isinstance(value, dict) or set(value) != {"manifest", "documents"} or not isinstance(value["documents"], list):
            raise FormalBriefDomainError("SourceManifest 必须含 manifest 与 documents")
        documents = tuple(FrozenDocument.from_mapping(item) for item in value["documents"])
        if not documents or len({item.source_document_id for item in documents}) != len(documents) or len({item.relative_path for item in documents}) != len(documents):
            raise FormalBriefDomainError("冻结输入不能为空且不得有重复文件")
        return cls(ArtifactPayload.from_mapping(value["manifest"]), documents)

    def require_exact_extracted_set(self, extracted: dict) -> None:
        sources = extracted.get("source_documents") if isinstance(extracted, dict) else None
        if not isinstance(sources, list):
            raise FormalBriefDomainError("提取结果缺少 source_documents")
        actual = []
        for source in sources:
            if not isinstance(source, dict):
                raise FormalBriefDomainError("提取来源无效")
            if type(source.get("critical")) is not bool:
                raise FormalBriefDomainError("提取来源必须保留冻结 critical")
            actual.append((str(source.get("document_id") or ""), str(source.get("sha256") or "").lower(), source["critical"]))
        expected = [(item.source_document_id, item.content_sha256, item.critical) for item in self.documents]
        if len(actual) != len(set(actual)) or set(actual) != set(expected):
            raise FormalBriefDomainError("导入集合必须与冻结 manifest 完整且精确一致")
        extractions = extracted.get("extractions")
        if not isinstance(extractions, list):
            raise FormalBriefDomainError("提取结果缺少 extractions")
        critical_by_document = {item.source_document_id: item.critical for item in self.documents}
        extracted_critical = []
        for extraction in extractions:
            if not isinstance(extraction, dict) or type(extraction.get("critical")) is not bool:
                raise FormalBriefDomainError("文档提取必须保留冻结 critical")
            document_id = str(extraction.get("document_id") or "")
            if document_id not in critical_by_document or extraction["critical"] != critical_by_document[document_id]:
                raise FormalBriefDomainError("冻结关键文件不能在提取结果中降级")
            extracted_critical.append(document_id)
        if len(extracted_critical) != len(set(extracted_critical)) or set(extracted_critical) != set(critical_by_document):
            raise FormalBriefDomainError("每个冻结文件必须恰有一个提取结果")

    def critical_for(self, source_document_id: str) -> bool:
        for document in self.documents:
            if document.source_document_id == source_document_id:
                return document.critical
        raise FormalBriefDomainError("提取结果引用未冻结文件")


@dataclass(frozen=True)
class EvidenceRef:
    artifact_ref: str
    source_document_id: str
    location: dict
    content_sha256: str

    @classmethod
    def from_mapping(cls, value: dict) -> "EvidenceRef":
        required = {"artifact_ref", "source_document_id", "location", "content_sha256"}
        if not isinstance(value, dict) or set(value) != required or not isinstance(value["location"], dict):
            raise FormalBriefDomainError("EvidenceRef 合同无效")
        ref, source, digest = str(value["artifact_ref"] or "").strip(), str(value["source_document_id"] or "").strip(), str(value["content_sha256"] or "").lower()
        if not ref.startswith("artifact://") or not source or not _is_sha256(digest) or not _valid_location(value["location"]):
            raise FormalBriefDomainError("EvidenceRef 必须含来源文件、完整位置和内容 SHA-256")
        return cls(ref, source, _copy(value["location"]), digest)

    def as_mapping(self) -> dict:
        return {"artifact_ref": self.artifact_ref, "source_document_id": self.source_document_id, "location": _copy(self.location), "content_sha256": self.content_sha256}


@dataclass(frozen=True)
class ConfirmedBriefInput:
    workspace_id: str
    project_id: str
    brief_version_id: str
    confirmation_id: str
    brief_snapshot_id: str
    payload: ArtifactPayload
    confirmation_status: str
    unresolved_conflict_count: int
    confirmed_by_actor_id: str
    confirmed_at: str

    @classmethod
    def from_mapping(cls, value: dict) -> "ConfirmedBriefInput":
        if not isinstance(value, dict) or set(value) != _CONFIRMED_INPUT_FIELDS:
            raise FormalBriefDomainError("ConfirmedBriefInput 字段必须完整且严格匹配")
        if type(value["unresolved_conflict_count"]) is not int or value["unresolved_conflict_count"] < 0:
            raise FormalBriefDomainError("unresolved_conflict_count 必须是非负整数")
        fields = {key: str(value[key] or "").strip() for key in _CONFIRMED_INPUT_FIELDS - {"payload", "unresolved_conflict_count"}}
        if not all(fields.values()) or fields["confirmation_status"] != "confirmed" or not _UTC_Z.fullmatch(fields["confirmed_at"]):
            raise FormalBriefDomainError("确认状态必须为 confirmed，confirmed_at 必须是 UTC Z 时间")
        return cls(
            workspace_id=fields["workspace_id"], project_id=fields["project_id"], brief_version_id=fields["brief_version_id"],
            confirmation_id=fields["confirmation_id"], brief_snapshot_id=fields["brief_snapshot_id"],
            payload=ArtifactPayload.from_mapping(value["payload"]), confirmation_status=fields["confirmation_status"],
            unresolved_conflict_count=value["unresolved_conflict_count"], confirmed_by_actor_id=fields["confirmed_by_actor_id"], confirmed_at=fields["confirmed_at"],
        )

    def as_mapping(self) -> dict:
        return {
            "workspace_id": self.workspace_id, "project_id": self.project_id, "brief_version_id": self.brief_version_id,
            "confirmation_id": self.confirmation_id, "brief_snapshot_id": self.brief_snapshot_id, "payload": self.payload.as_mapping(),
            "confirmation_status": self.confirmation_status, "unresolved_conflict_count": self.unresolved_conflict_count,
            "confirmed_by_actor_id": self.confirmed_by_actor_id, "confirmed_at": self.confirmed_at,
        }


@dataclass(frozen=True)
class ConfirmationBinding:
    """Issued only by the future same-transaction snapshot/confirmation writer."""

    confirmation_id: str
    brief_snapshot_id: str

    def __post_init__(self) -> None:
        if not self.confirmation_id.strip() or not self.brief_snapshot_id.strip():
            raise FormalBriefDomainError("确认必须由同事务绑定有效 brief_snapshot_id")


@dataclass(frozen=True)
class PendingBriefVersion:
    workspace_id: str
    project_id: str
    created_by_actor_id: str
    brief_version_id: str
    parent_brief_version_id: str | None
    source_manifest: SourceManifest
    payload: ArtifactPayload
    payload_data: dict
    unresolved_conflict_count: int
    critical_failure_count: int


@dataclass(frozen=True)
class LegacyBriefSnapshot:
    workspace_id: str
    project_id: str
    brief_snapshot_id: str
    payload: ArtifactPayload
    source_kind: str = "legacy_single_source"


@dataclass(frozen=True)
class T02ConfirmedBriefInput:
    """Thin non-dict adapter: T02 keeps the confirmed authority beside payload."""

    confirmed: ConfirmedBriefInput
    brief_payload: dict


class ArtifactWriter(Protocol):
    def write(self, payload: bytes) -> ArtifactPayload: ...
    def read(self, payload: ArtifactPayload) -> bytes: ...


class ConfirmedBriefAuthority(Protocol):
    """Implemented by T00/I00 inside one workspace/schema transaction."""

    def load_confirmed(self, value: ConfirmedBriefInput) -> ConfirmedBriefInput: ...
    def read_payload_bytes(self, payload: ArtifactPayload) -> bytes: ...


class ProjectAccessPort(Protocol):
    def require_project_permission(self, auth_token: str, *, workspace_id: str, project_id: str, permission: str) -> str: ...


class FormalBriefDomainService:
    def __init__(self, artifact_writer: ArtifactWriter, id_factory):
        self.artifact_writer, self.id_factory = artifact_writer, id_factory

    def imported_version(
        self,
        *,
        workspace_id: str,
        project_id: str,
        source_manifest: SourceManifest,
        extracted: dict,
        created_by_actor_id: str,
        parent_brief_version_id: str | None = None,
        parent_version: PendingBriefVersion | None = None,
    ) -> PendingBriefVersion:
        source_manifest.require_exact_extracted_set(extracted)
        payload = formalize_alpha_extraction(extracted)
        if parent_version is not None:
            if (parent_version.workspace_id, parent_version.project_id, parent_version.brief_version_id) != (workspace_id, project_id, parent_brief_version_id):
                raise FormalBriefDomainError("导入父版本身份不一致")
            previous = self._authoritative_payload(parent_version)
            if previous.get("field_updates"):
                payload["field_updates"] = _copy(previous["field_updates"])
            # A new file does not revoke a choice for an unchanged field.
            # Reuse its exact facts and evidence so the original decision remains verifiable.
            for field in {item["field_path"] for item in previous["conflicts"]
                          if item.get("resolution_status") == "resolved"}:
                before = [item for item in previous["facts"] if item["field_path"] == field]
                after = [item for item in payload["facts"] if item["field_path"] == field]
                signature = lambda rows: sorted(_json({"value": row["value_json"], "evidence": row["evidence_refs"]}) for row in rows)
                if signature(before) != signature(after):
                    continue
                payload["facts"] = [item for item in payload["facts"] if item["field_path"] != field] + before
                payload["conflicts"] = [item for item in payload["conflicts"] if item["field_path"] != field] + [item for item in previous["conflicts"] if item["field_path"] == field]
        return self._new_version(
            workspace_id, project_id, source_manifest,
            payload, parent_brief_version_id,
            created_by_actor_id,
            rekey_imported_entities=True,
        )

    def replaced_field_version(self, parent: PendingBriefVersion, *, source_manifest: SourceManifest,
                               extracted: dict, field_path: str, created_by_actor_id: str) -> PendingBriefVersion:
        source_manifest.require_exact_extracted_set(extracted)
        fresh = formalize_alpha_extraction(extracted)
        if (fresh.get("project_id") != parent.project_id or len(fresh["facts"]) != 1
                or fresh["facts"][0]["field_path"] != field_path or fresh["conflicts"]):
            raise FormalBriefDomainError("单字段修改必须提供唯一目标事实及来源")
        payload = self._authoritative_payload(parent)
        for key in ("source_documents", "extractions", "images"):
            payload[key] = fresh[key]
        payload["facts"] = [item for item in payload["facts"] if item["field_path"] != field_path] + fresh["facts"]
        payload["conflicts"] = [item for item in payload["conflicts"] if item["field_path"] != field_path]
        updated_sources = list(dict.fromkeys(ref["source_document_id"] for ref in fresh["facts"][0]["evidence_refs"]))
        literal_sources = list(dict.fromkeys([
            *(source for item in payload.get("field_updates", []) if item["field_path"] == field_path
              for source in item.get("literal_source_document_ids", item["source_document_ids"])),
            *updated_sources,
        ]))
        payload["field_updates"] = [item for item in payload.get("field_updates", []) if item["field_path"] != field_path] + [{
            "field_path": field_path,
            "source_document_ids": updated_sources,
            "literal_source_document_ids": literal_sources,
            "retired_source_document_ids": [item.source_document_id for item in parent.source_manifest.documents
                                            if item.source_document_id not in updated_sources],
        }]
        return self._new_version(parent.workspace_id, parent.project_id, source_manifest, payload,
                                 parent.brief_version_id, created_by_actor_id, rekey_imported_entities=True)

    def resolve_conflicts(self, version: PendingBriefVersion, *, resolutions: dict[str, tuple[str, ArtifactPayload]], actor_id: str, resolved_at: str) -> PendingBriefVersion:
        if not isinstance(resolutions, dict) or not actor_id or not _UTC_Z.fullmatch(str(resolved_at or "")):
            raise FormalBriefDomainError("冲突解决人、UTC 时间或证据无效")
        payload, changed = self._authoritative_payload(version), False
        fact_ids = {fact["fact_id"]: self.id_factory("brief_fact") for fact in payload["facts"]}
        for fact in payload["facts"]:
            fact["fact_id"] = fact_ids[fact["fact_id"]]
        for conflict in payload["conflicts"]:
            original_conflict_id, original_fact_ids = conflict["conflict_id"], list(conflict["fact_ids"])
            choice = resolutions.get(original_conflict_id)
            conflict["conflict_id"], conflict["fact_ids"] = self.id_factory("brief_conflict"), [fact_ids[fact_id] for fact_id in original_fact_ids]
            if conflict.get("resolution_status") == "resolved":
                conflict["resolved_fact_id"] = fact_ids[conflict["resolved_fact_id"]]
            if choice is None:
                continue
            fact_id, evidence = choice
            if fact_id not in original_fact_ids:
                raise FormalBriefDomainError("resolved_fact_id 不属于当前冲突候选")
            conflict.update({"resolution_status": "resolved", "resolved_fact_id": fact_ids[fact_id], "resolved_by_actor_id": actor_id, "resolved_at": resolved_at, "resolution_evidence": evidence.as_mapping()})
            changed = True
        if not changed:
            raise FormalBriefDomainError("至少需要解决一个已记录冲突")
        return self._new_version(version.workspace_id, version.project_id, version.source_manifest, payload, version.brief_version_id, actor_id)

    def confirm(self, version: PendingBriefVersion, *, actor_id: str, confirmed_at: str, binding: ConfirmationBinding) -> ConfirmedBriefInput:
        if not actor_id or not _UTC_Z.fullmatch(str(confirmed_at or "")):
            raise FormalBriefDomainError("确认人不能为空且 confirmed_at 必须是 UTC Z 时间")
        payload = self._authoritative_payload(version)
        if _unresolved_conflict_count(payload) or _critical_failure_count(payload, version.source_manifest):
            raise FormalBriefDomainError("存在未解决冲突或关键文件提取失败，Brief 不可确认")
        return ConfirmedBriefInput(
            workspace_id=version.workspace_id, project_id=version.project_id, brief_version_id=version.brief_version_id,
            confirmation_id=binding.confirmation_id, brief_snapshot_id=binding.brief_snapshot_id, payload=version.payload,
            confirmation_status="confirmed", unresolved_conflict_count=0, confirmed_by_actor_id=actor_id, confirmed_at=confirmed_at,
        )

    def _authoritative_payload(self, version: PendingBriefVersion) -> dict:
        if not isinstance(version, PendingBriefVersion):
            raise TypeError("确认只接受 PendingBriefVersion")
        try:
            raw = self.artifact_writer.read(version.payload)
        except (OSError, KeyError, TypeError) as exc:
            raise FormalBriefDomainError("Brief artifact 不可读取") from exc
        if sha256(raw).hexdigest() != version.payload.sha256:
            raise FormalBriefDomainError("Brief artifact SHA-256 与版本记录不一致")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FormalBriefDomainError("Brief artifact 无法解析") from exc
        if not isinstance(envelope, dict) or envelope.get("schema_version") != FORMAL_BRIEF_SCHEMA or any(envelope.get(key) != getattr(version, key) for key in ("workspace_id", "project_id", "brief_version_id")) or not isinstance(envelope.get("brief"), dict):
            raise FormalBriefDomainError("Brief artifact 与版本身份不一致")
        payload = envelope["brief"]
        version.source_manifest.require_exact_extracted_set(payload)
        return _copy(payload)

    def _new_version(self, workspace_id, project_id, manifest, payload, parent, actor_id, *, rekey_imported_entities: bool = False) -> PendingBriefVersion:
        if not all(str(item or "").strip() for item in (workspace_id, project_id, actor_id)):
            raise FormalBriefDomainError("workspace_id、project_id 与 actor_id 不能为空")
        if rekey_imported_entities:
            payload = _rekey_imported_entities(payload, self.id_factory)
        _validate_formal_payload(payload, project_id, manifest)
        version_id = self.id_factory("brief_version")
        encoded = _json({"schema_version": FORMAL_BRIEF_SCHEMA, "workspace_id": workspace_id, "project_id": project_id, "brief_version_id": version_id, "brief": payload}).encode("utf-8")
        artifact = self.artifact_writer.write(encoded)
        if sha256(encoded).hexdigest() != artifact.sha256:
            raise FormalBriefDomainError("Artifact writer 未返回内容 SHA-256")
        return PendingBriefVersion(workspace_id, project_id, actor_id, version_id, parent, manifest, artifact, _copy(payload), _unresolved_conflict_count(payload), _critical_failure_count(payload, manifest))


class FormalBriefWorkflow:
    """Pure orchestration; concrete ports must use one DB transaction at integration."""

    def __init__(self, domain: FormalBriefDomainService, access: ProjectAccessPort):
        self.domain, self.access = domain, access

    def import_frozen(self, auth_token: str, *, workspace_id: str, project_id: str, source_manifest: SourceManifest, extracted: dict) -> PendingBriefVersion:
        actor = self.access.require_project_permission(auth_token, workspace_id=workspace_id, project_id=project_id, permission="project.update")
        return self.domain.imported_version(workspace_id=workspace_id, project_id=project_id, source_manifest=source_manifest, extracted=extracted, created_by_actor_id=actor)

    def resolve_conflicts(self, auth_token: str, *, version: PendingBriefVersion, resolutions: dict[str, tuple[str, ArtifactPayload]], resolved_at: str) -> PendingBriefVersion:
        actor = self.access.require_project_permission(auth_token, workspace_id=version.workspace_id, project_id=version.project_id, permission="project.update")
        return self.domain.resolve_conflicts(version, resolutions=resolutions, actor_id=actor, resolved_at=resolved_at)

    def confirm(self, auth_token: str, *, version: PendingBriefVersion, confirmed_at: str, binding: ConfirmationBinding) -> ConfirmedBriefInput:
        actor = self.access.require_project_permission(auth_token, workspace_id=version.workspace_id, project_id=version.project_id, permission="review.record")
        return self.domain.confirm(version, actor_id=actor, confirmed_at=confirmed_at, binding=binding)


class ConfirmedBriefResolver:
    def __init__(self, authority: ConfirmedBriefAuthority):
        self.authority = authority

    def resolve_payload(self, value: ConfirmedBriefInput) -> dict:
        if not isinstance(value, ConfirmedBriefInput):
            raise TypeError("Resolver 只接受 ConfirmedBriefInput，拒绝裸 dict")
        if value.confirmation_status != "confirmed" or value.unresolved_conflict_count != 0 or not _UTC_Z.fullmatch(value.confirmed_at):
            raise FormalBriefDomainError("仅允许无未解决冲突、UTC 确认的 Brief")
        if self.authority.load_confirmed(value) != value:
            raise FormalBriefDomainError("ConfirmedBriefInput 与持久确认记录不一致")
        raw = self.authority.read_payload_bytes(value.payload)
        if sha256(raw).hexdigest() != value.payload.sha256:
            raise FormalBriefDomainError("Resolver 检出 payload SHA-256 篡改")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FormalBriefDomainError("payload bytes 无法解析") from exc
        if not isinstance(envelope, dict) or any(envelope.get(key) != getattr(value, key) for key in ("workspace_id", "project_id", "brief_version_id")) or envelope.get("schema_version") != FORMAL_BRIEF_SCHEMA or not isinstance(envelope.get("brief"), dict):
            raise FormalBriefDomainError("payload schema、项目或 Brief version 不匹配")
        _validate_confirmed_resolution_evidence(
            envelope["brief"], value=value, authority=self.authority
        )
        return envelope["brief"]


def _validate_confirmed_resolution_evidence(
    payload: dict, *, value: ConfirmedBriefInput, authority: ConfirmedBriefAuthority
) -> None:
    """Bind every resolved conflict to its immutable human resolution artifact."""
    facts = payload.get("facts")
    conflicts = payload.get("conflicts", [])
    if not isinstance(facts, list) or not isinstance(conflicts, list):
        raise FormalBriefDomainError("Confirmed Brief facts 或 conflicts 合同无效")
    fact_by_id = {
        str(fact.get("fact_id") or ""): fact
        for fact in facts if isinstance(fact, dict) and str(fact.get("fact_id") or "")
    }
    if len(fact_by_id) != len(facts):
        raise FormalBriefDomainError("Confirmed Brief fact_id 缺失或重复")
    claimed_candidates: set[str] = set()
    required_v1 = {
        "schema_version", "workspace_id", "project_id", "brief_version_id",
        "conflict_id", "selected_fact_id", "conflict_evidence_refs",
        "selected_fact_evidence_refs", "resolved_by_actor_id", "resolved_at",
    }
    required_v2 = {*required_v1, "selected_fact_value_sha256"}
    for conflict in conflicts:
        if not isinstance(conflict, dict) or conflict.get("resolution_status") != "resolved":
            raise FormalBriefDomainError("Confirmed Brief 仍含未解决冲突")
        candidate_ids = conflict.get("fact_ids")
        selected_id = str(conflict.get("resolved_fact_id") or "")
        if (
            not isinstance(candidate_ids, list)
            or len(candidate_ids) < 2
            or len(set(candidate_ids)) != len(candidate_ids)
            or selected_id not in candidate_ids
            or any(candidate not in fact_by_id for candidate in candidate_ids)
            or any(candidate in claimed_candidates for candidate in candidate_ids)
        ):
            raise FormalBriefDomainError("Confirmed Brief 冲突候选或 resolved_fact_id 无效")
        claimed_candidates.update(candidate_ids)
        field_path = str(conflict.get("field_path") or "")
        if not field_path or any(
            str(fact_by_id[candidate].get("field_path") or "") != field_path
            for candidate in candidate_ids
        ):
            raise FormalBriefDomainError("Confirmed Brief 冲突字段与候选事实不一致")
        actor_id = str(conflict.get("resolved_by_actor_id") or "")
        resolved_at = str(conflict.get("resolved_at") or "")
        if not actor_id or not _UTC_Z.fullmatch(resolved_at):
            raise FormalBriefDomainError("Confirmed Brief 冲突解决人或时间无效")
        evidence = ArtifactPayload.from_mapping(conflict.get("resolution_evidence") or {})
        raw = authority.read_payload_bytes(evidence)
        if sha256(raw).hexdigest() != evidence.sha256:
            raise FormalBriefDomainError("冲突解决证据 SHA-256 已漂移")
        try:
            resolution = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FormalBriefDomainError("冲突解决证据无法解析") from exc
        if (
            not isinstance(resolution, dict)
            or frozenset(resolution) not in {frozenset(required_v1), frozenset(required_v2)}
        ):
            raise FormalBriefDomainError("冲突解决证据字段不完整或含未知字段")
        selected_fact = fact_by_id[selected_id]
        selected_refs = selected_fact.get("evidence_refs")
        evidence_schema = str(resolution.get("schema_version") or "")
        selected_value_sha256 = sha256(
            str(selected_fact.get("value_json") or "").encode("utf-8")
        ).hexdigest()
        matching_candidates = [
            candidate for candidate in candidate_ids
            if fact_by_id[candidate].get("evidence_refs") == resolution.get("selected_fact_evidence_refs")
            and (
                evidence_schema == "mplus-formal-resolution-evidence/v1"
                or sha256(
                    str(fact_by_id[candidate].get("value_json") or "").encode("utf-8")
                ).hexdigest() == resolution.get("selected_fact_value_sha256")
            )
        ]
        if (
            evidence_schema not in {
                "mplus-formal-resolution-evidence/v1",
                "mplus-formal-resolution-evidence/v2",
            }
            or resolution.get("workspace_id") != value.workspace_id
            or resolution.get("project_id") != value.project_id
            or not str(resolution.get("brief_version_id") or "")
            or not str(resolution.get("conflict_id") or "")
            or not str(resolution.get("selected_fact_id") or "")
            or resolution.get("resolved_by_actor_id") != actor_id
            or resolution.get("resolved_at") != resolved_at
            or resolution.get("conflict_evidence_refs") != conflict.get("evidence_refs")
            or resolution.get("selected_fact_evidence_refs") != selected_refs
            or (
                evidence_schema == "mplus-formal-resolution-evidence/v2"
                and resolution.get("selected_fact_value_sha256") != selected_value_sha256
            )
            or matching_candidates != [selected_id]
        ):
            raise FormalBriefDomainError("冲突选择、人工证据、解决人或时间发生漂移")


def adapt_confirmed_brief_for_t02(value: ConfirmedBriefInput, resolver: ConfirmedBriefResolver) -> T02ConfirmedBriefInput:
    """The sole T02 entry: it cannot bypass confirmation with a bare mapping."""
    if not isinstance(value, ConfirmedBriefInput):
        raise TypeError("T02 适配只接受 ConfirmedBriefInput")
    return T02ConfirmedBriefInput(confirmed=value, brief_payload=resolver.resolve_payload(value))


def _rekey_imported_entities(payload: dict, id_factory) -> dict:
    """Make content-derived input aliases safe for globally keyed formal rows."""
    value = _copy(payload)
    facts = value.get("facts")
    conflicts = value.get("conflicts")
    if not isinstance(facts, list) or not isinstance(conflicts, list):
        raise FormalBriefDomainError("BriefFact 或 BriefConflict 合同无效")
    fact_ids: dict[str, str] = {}
    issued_fact_ids: set[str] = set()
    for fact in facts:
        if not isinstance(fact, dict):
            raise FormalBriefDomainError("BriefFact 合同无效")
        original_id = str(fact.get("fact_id") or "")
        if not original_id or original_id in fact_ids:
            raise FormalBriefDomainError("BriefFact ID 缺失或重复")
        fact_ids[original_id] = _issued_entity_id(id_factory, "brief_fact", issued_fact_ids)
    for fact in facts:
        fact["fact_id"] = fact_ids[str(fact["fact_id"])]
    issued_conflict_ids: set[str] = set()
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            raise FormalBriefDomainError("BriefConflict 合同无效")
        original_ids = conflict.get("fact_ids")
        if not isinstance(original_ids, list) or any(
            not isinstance(item, str) or item not in fact_ids for item in original_ids
        ):
            raise FormalBriefDomainError("BriefConflict 候选事实无效")
        conflict["conflict_id"] = _issued_entity_id(id_factory, "brief_conflict", issued_conflict_ids)
        conflict["fact_ids"] = [fact_ids[item] for item in original_ids]
        if conflict.get("resolution_status") == "resolved":
            selected = conflict.get("resolved_fact_id")
            if selected not in original_ids:
                raise FormalBriefDomainError("已采用事实不属于冲突候选")
            conflict["resolved_fact_id"] = fact_ids[selected]
    return value


def _issued_entity_id(id_factory, prefix: str, issued: set[str]) -> str:
    identifier = id_factory(prefix)
    if not isinstance(identifier, str) or not identifier.strip() or identifier in issued:
        raise FormalBriefDomainError("ID factory 必须生成非空且唯一的实体 ID")
    issued.add(identifier)
    return identifier


def formalize_alpha_extraction(extracted: dict) -> dict:
    """Turn Alpha locations into formal evidence records with verifiable SHA."""
    if not isinstance(extracted, dict):
        raise FormalBriefDomainError("Alpha 提取结果必须是 object")
    sources = {item.get("document_id"): item for item in extracted.get("source_documents", []) if isinstance(item, dict)}
    evidence_by_ref = {}
    extractions = []
    for extraction in extracted.get("extractions", []):
        source = sources.get(extraction.get("document_id"))
        if source is None:
            raise FormalBriefDomainError("提取结果缺少来源文件")
        locations = []
        for location in extraction.get("locations", []):
            evidence = EvidenceRef.from_mapping({"artifact_ref": location["evidence_ref"], "source_document_id": source["document_id"], "location": _formal_location(location["location"]), "content_sha256": sha256(location["text"].encode("utf-8")).hexdigest()})
            locations.append({"evidence": evidence.as_mapping(), "text": location["text"]})
            evidence_by_ref[location["evidence_ref"]] = evidence.as_mapping()
        formal_extraction = {
            "document_id": source["document_id"], "status": extraction["status"],
            "critical": extraction["critical"], "locations": locations,
            "error": _copy(extraction.get("error")),
        }
        if "ocr" in extraction:
            formal_extraction["ocr"] = _validated_ocr_evidence(
                extraction["ocr"], str(source.get("sha256") or ""),
            )
        extractions.append(formal_extraction)
    facts = []
    for fact in extracted.get("facts", []):
        refs = [evidence_by_ref.get(ref) for ref in fact.get("evidence_refs", [])]
        if not refs or any(ref is None for ref in refs):
            raise FormalBriefDomainError("事实必须引用已验证的 EvidenceRef")
        facts.append({"fact_id": fact["fact_id"], "field_path": fact["field"], "value_json": _json(fact["value"]), "certainty": "confirmed" if fact.get("confidence") == "direct" else "pending", "confidence": "high" if fact.get("confidence") == "direct" else "medium", "evidence_refs": refs,
                      **({"semantic_metadata": _copy(fact["semantic_metadata"])} if fact.get("semantic_metadata") else {})})
    fact_evidence = {fact["fact_id"]: fact["evidence_refs"] for fact in facts}
    conflicts = []
    for conflict in extracted.get("conflicts", []):
        refs = [ref for fact_id in conflict["fact_ids"] for ref in fact_evidence.get(fact_id, [])]
        if not refs:
            raise FormalBriefDomainError("冲突必须保留候选事实证据")
        conflicts.append({"conflict_id": conflict["conflict_id"], "field_path": conflict["field"], "fact_ids": list(conflict["fact_ids"]), "evidence_refs": refs, "resolution_status": "unresolved", "resolved_fact_id": None, "resolved_by_actor_id": None, "resolved_at": None, "resolution_evidence": None})
    return {"project_id": extracted.get("project_id"), "source_documents": _copy(extracted.get("source_documents", [])), "extractions": extractions, "images": _formal_images(extracted.get("images", []), sources), "facts": facts, "conflicts": conflicts}


def legacy_snapshot_view(*, workspace_id: str, project_id: str, brief_snapshot_id: str, payload: dict) -> LegacyBriefSnapshot:
    return LegacyBriefSnapshot(str(workspace_id or "").strip(), str(project_id or "").strip(), str(brief_snapshot_id or "").strip(), ArtifactPayload.from_mapping(payload))


def _formal_images(images, sources) -> list[dict]:
    by_sha = {}
    for image in images:
        digest = str(image.get("sha256") or "").lower()
        if not _is_sha256(digest):
            raise FormalBriefDomainError("图片 SHA-256 无效")
        raw_image_ref = str(image.get("evidence_ref") or f"artifact://formal-image/{digest}")
        item = by_sha.setdefault(digest, {"image_id": image["image_id"], "content_sha256": digest, "artifact": {"ref": raw_image_ref, "sha256": digest}, "source_links": []})
        if item["artifact"]["ref"] != raw_image_ref:
            raise FormalBriefDomainError("同 SHA 图片 ArtifactRef 不一致")
        links = image.get("source_links") or [{"source_document_id": image.get("source_document_id"), "location": image.get("location")}]
        for link in links:
            source_document_id, location = str(link.get("source_document_id") or ""), _formal_location(link.get("location") or {})
            if source_document_id not in sources:
                raise FormalBriefDomainError("图片缺少 source_document_id")
            raw_ref = str(link.get("evidence_ref") or image.get("evidence_ref") or raw_image_ref)
            evidence = EvidenceRef.from_mapping({"artifact_ref": raw_ref, "source_document_id": source_document_id, "location": location, "content_sha256": digest})
            link_value = {"source_document_id": source_document_id, "evidence": evidence.as_mapping()}
            if link_value not in item["source_links"]:
                item["source_links"].append(link_value)
    return list(by_sha.values())


def _validate_formal_payload(payload, project_id, manifest) -> None:
    if not isinstance(payload, dict) or payload.get("project_id") != project_id:
        raise FormalBriefDomainError("提取结果项目身份无效")
    manifest.require_exact_extracted_set(payload)
    updates = payload.get("field_updates", [])
    if not isinstance(updates, list):
        raise FormalBriefDomainError("字段修改来源范围无效")
    updated_fields = set()
    for update in updates:
        if not isinstance(update, dict) or set(update) != {"field_path", "source_document_ids", "retired_source_document_ids", "literal_source_document_ids"}:
            raise FormalBriefDomainError("字段修改来源范围无效")
        field = update["field_path"]
        sources, retired = update["source_document_ids"], update["retired_source_document_ids"]
        literals = update["literal_source_document_ids"]
        if (not isinstance(field, str) or not field or field in updated_fields
                or not isinstance(sources, list) or not sources or not isinstance(retired, list)
                or any(not isinstance(item, str) or not item for item in [*sources, *retired])
                or len(sources) != len(set(sources)) or len(retired) != len(set(retired))
                or set(sources) & set(retired)
                or not isinstance(literals, list) or any(not isinstance(item, str) or not item for item in literals)
                or len(literals) != len(set(literals)) or not set(sources).issubset(set(literals))):
            raise FormalBriefDomainError("字段修改来源范围无效")
        field_sources = {ref.get("source_document_id") for fact in payload.get("facts", [])
                         if fact.get("field_path") == field for ref in fact.get("evidence_refs", [])}
        manifest_sources = {item.source_document_id for item in manifest.documents}
        if not set(sources).issubset(manifest_sources & field_sources):
            raise FormalBriefDomainError("字段修改来源未绑定当前字段事实与来源清单")
        updated_fields.add(field)
    source_hashes = {item.source_document_id: item.content_sha256 for item in manifest.documents}
    for extraction in payload["extractions"]:
        if extraction.get("status") not in {"succeeded", "failed"} or type(extraction.get("critical")) is not bool:
            raise FormalBriefDomainError("文档提取状态无效")
        for location in extraction.get("locations", []):
            EvidenceRef.from_mapping(location["evidence"])
        if "ocr" in extraction:
            _validated_ocr_evidence(extraction["ocr"], source_hashes[extraction["document_id"]])
    for image in payload["images"]:
        ArtifactPayload.from_mapping(image.get("artifact") or {})
        if not _is_sha256(str(image.get("content_sha256") or "")) or not image.get("source_links"):
            raise FormalBriefDomainError("正式图片记录必须有 SHA 和全部来源关联")
        for link in image["source_links"]:
            EvidenceRef.from_mapping(link.get("evidence") or {})
    fact_ids = set()
    for fact in payload["facts"]:
        if not str(fact.get("fact_id") or "") or fact.get("certainty") not in {"confirmed", "pending"} or fact.get("confidence") not in {"high", "medium", "low"}:
            raise FormalBriefDomainError("BriefFact 合同无效")
        fact_ids.add(fact["fact_id"])
        for evidence in fact.get("evidence_refs", []): EvidenceRef.from_mapping(evidence)
    for conflict in payload["conflicts"]:
        if conflict.get("resolution_status") not in {"unresolved", "resolved"} or not set(conflict.get("fact_ids", [])).issubset(fact_ids):
            raise FormalBriefDomainError("BriefConflict 合同无效")
        for evidence in conflict.get("evidence_refs", []): EvidenceRef.from_mapping(evidence)
        if conflict["resolution_status"] == "resolved":
            if conflict.get("resolved_fact_id") not in conflict["fact_ids"] or not conflict.get("resolved_by_actor_id") or not _UTC_Z.fullmatch(str(conflict.get("resolved_at") or "")):
                raise FormalBriefDomainError("冲突解决记录无效")
            ArtifactPayload.from_mapping(conflict.get("resolution_evidence") or {})


def _validated_ocr_evidence(records, source_sha256: str) -> list[dict]:
    """Retain document transcription evidence without converting it to confirmed facts."""
    if not isinstance(records, list):
        raise FormalBriefDomainError("OCR 证据必须是列表")
    required = {
        "status", "location", "provider", "model", "source_sha256", "image_sha256",
        "text_sha256", "raw_response_sha256", "evidence",
    }
    for item in records:
        if not isinstance(item, dict) or not required <= set(item):
            raise FormalBriefDomainError("OCR 证据字段不完整")
        if item["status"] not in {"succeeded", "no_text", "failed", "unavailable"}:
            raise FormalBriefDomainError("OCR 证据状态无效")
        if not isinstance(item["location"], dict) or not _valid_location(_formal_location(item["location"])):
            raise FormalBriefDomainError("OCR 证据缺少文档内位置")
        if item["source_sha256"] != source_sha256 or not _is_sha256(source_sha256):
            raise FormalBriefDomainError("OCR 证据与原文件 SHA 不一致")
        for key in ("text_sha256", "raw_response_sha256"):
            if not _is_sha256(str(item[key] or "")):
                raise FormalBriefDomainError("OCR 文字或响应 SHA 无效")
        if not all(isinstance(item[key], str) for key in ("provider", "model")):
            raise FormalBriefDomainError("OCR 来源类型无效")
        evidence = ArtifactPayload.from_mapping(item["evidence"])
        if not evidence.ref.startswith("artifact://formal-input-ocr/"):
            raise FormalBriefDomainError("OCR 证据引用无效")
        image_sha = item["image_sha256"]
        if image_sha is not None and not _is_sha256(str(image_sha)):
            raise FormalBriefDomainError("OCR 图片 SHA 无效")
        if item.get("image_artifact") is not None:
            image = ArtifactPayload.from_mapping(item["image_artifact"])
            if image.sha256 != image_sha or not image.ref.startswith("artifact://formal-input-ocr-images/"):
                raise FormalBriefDomainError("OCR 图片引用与 SHA 不一致")
        if item["status"] in {"succeeded", "no_text"} and (
            not image_sha or not item["provider"] or not item["model"] or not item.get("image_artifact")
        ):
            raise FormalBriefDomainError("成功 OCR 必须保留实际图片及识别来源")
        if item.get("error") is not None and (
            not isinstance(item["error"], dict)
            or not isinstance(item["error"].get("code"), str)
            or not isinstance(item["error"].get("message"), str)
        ):
            raise FormalBriefDomainError("OCR 失败信息无效")
    return _copy(records)


def _valid_location(location: dict) -> bool:
    if all(type(location.get(key)) is int and location[key] > 0 for key in ("row", "column")) and str(location.get("sheet") or "").strip(): return True
    return any(type(location.get(key)) is int and location[key] > 0 for key in ("page", "paragraph", "slide", "image", "image_index"))


def _unresolved_conflict_count(payload: dict) -> int:
    return sum(row.get("resolution_status") == "unresolved" for row in payload.get("conflicts", []))


def _critical_failure_count(payload: dict, manifest: SourceManifest) -> int:
    return sum(row.get("status") == "failed" and manifest.critical_for(str(row.get("document_id") or "")) for row in payload.get("extractions", []))


def _formal_location(location: dict) -> dict:
    result = _copy(location)
    if "image" in result:
        result["image_index"] = result.pop("image")
    if "embedded_image" in result:
        result["image_index"] = result.pop("embedded_image")
    return result


def _is_sha256(value: str) -> bool: return len(value) == 64 and all(char in "0123456789abcdef" for char in value)
def _json(value) -> str: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def _copy(value): return json.loads(_json(value))
