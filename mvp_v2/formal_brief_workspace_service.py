"""WorkspaceStore authority adapter for immutable formal Brief versions."""

from __future__ import annotations

from contextvars import ContextVar
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Callable

from .formal_brief_domain import (
    FORMAL_BRIEF_SCHEMA,
    ArtifactPayload,
    ConfirmationBinding,
    ConfirmedBriefInput,
    ConfirmedBriefResolver,
    FormalBriefDomainError,
    FormalBriefDomainService,
    PendingBriefVersion,
    SourceManifest,
)
from .creative_standard import (
    CONFIRMED_CREATIVE_STANDARD_SCHEMA,
    ConfirmedCreativeStandardInput,
    ConfirmedCreativeStandardResolver,
    CreativeStandardArtifactStore,
    CreativeStandardDomainService,
    CreativeStandardVersion,
    PairedConfirmedBriefCreativeStandardInput,
)
from .identity_service import require_permission
from .mutation_policy import require_active_project
from .script_type_adapter import resolve_brief_category
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    new_id,
    require_nonempty,
    utc_now,
)


class FormalBriefArtifactStore:
    """Content-addressed payload storage; DB rows remain the only Brief authority."""

    def __init__(self, root: str | Path, *, namespace: str = "formal-brief"):
        if namespace not in {"formal-brief", "creative-draft", "creative-image"}:
            raise ValueError("Unsupported payload namespace")
        self.namespace = namespace
        self.root = Path(root).expanduser().resolve() / f"{namespace}-payloads"
        self._created: ContextVar[tuple[Path, ...]] = ContextVar("formal_brief_created_artifacts", default=())

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
                raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "正式 Brief artifact 内容已损坏")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if _write_once(target, payload):
                self._created.set((*self._created.get(), target))
            elif sha256(target.read_bytes()).hexdigest() != digest:
                raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "正式 Brief artifact 内容已损坏")
        return ArtifactPayload(f"artifact://{self.namespace}/{digest}", digest)

    def read(self, payload: ArtifactPayload) -> bytes:
        if not isinstance(payload, ArtifactPayload) or payload.ref != f"artifact://{self.namespace}/{payload.sha256}":
            raise WorkspaceServiceError("BRIEF_ARTIFACT_UNAVAILABLE", "正式 Brief artifact 引用无效")
        target = self.root / payload.sha256[:2] / f"{payload.sha256}.json"
        if not target.is_file():
            raise WorkspaceServiceError("BRIEF_ARTIFACT_UNAVAILABLE", "正式 Brief artifact 不存在")
        raw = target.read_bytes()
        if sha256(raw).hexdigest() != payload.sha256:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "正式 Brief artifact SHA-256 已变化")
        return raw


class WorkspaceConfirmedBriefAuthority:
    """Resolver authority backed by the same WorkspaceStore migration sequence."""

    def __init__(self, store: WorkspaceStore, artifacts: FormalBriefArtifactStore):
        self.store, self.artifacts = store, artifacts

    def load_confirmed(self, value: ConfirmedBriefInput) -> ConfirmedBriefInput:
        with self.store.connection() as connection:
            row = connection.execute(
                """
                SELECT c.*, v.payload_ref, v.payload_sha256
                FROM brief_confirmations c
                JOIN brief_versions v ON v.brief_version_id = c.brief_version_id
                WHERE c.confirmation_id = ?
                """,
                (value.confirmation_id,),
            ).fetchone()
        if row is None:
            raise WorkspaceServiceError("NOT_FOUND", "正式 Brief 确认记录不存在")
        stored = ConfirmedBriefInput(
            workspace_id=row["workspace_id"], project_id=row["project_id"],
            brief_version_id=row["brief_version_id"], confirmation_id=row["confirmation_id"],
            brief_snapshot_id=row["brief_snapshot_id"],
            payload=ArtifactPayload(row["payload_ref"], row["payload_sha256"]),
            confirmation_status=row["confirmation_status"],
            unresolved_conflict_count=row["unresolved_conflict_count"],
            confirmed_by_actor_id=row["confirmed_by_actor_id"], confirmed_at=row["confirmed_at"],
        )
        if stored != value:
            raise FormalBriefDomainError("ConfirmedBriefInput 与同库确认记录不一致")
        return stored

    def read_payload_bytes(self, payload: ArtifactPayload) -> bytes:
        return self.artifacts.read(payload)


class FormalBriefWorkspaceService:
    """One-transaction formal Brief import, resolution, confirmation, and read path."""

    def __init__(self, store: WorkspaceStore, *, artifact_root: str | Path):
        self.store = store
        self.store.initialize()
        self.artifacts = FormalBriefArtifactStore(artifact_root)
        self.domain = FormalBriefDomainService(self.artifacts, new_id)
        self.authority = WorkspaceConfirmedBriefAuthority(store, self.artifacts)
        self.creative_standard_artifacts = CreativeStandardArtifactStore(artifact_root)
        self.creative_standards = CreativeStandardDomainService(
            self.creative_standard_artifacts, new_id
        )

    def resolver(self) -> ConfirmedBriefResolver:
        return ConfirmedBriefResolver(self.authority)

    def creative_standard_resolver(self) -> ConfirmedCreativeStandardResolver:
        return ConfirmedCreativeStandardResolver(self)

    def preflight_import(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int,
    ) -> None:
        """Authorize and check the project lock before an upload byte is read."""
        with self.store.connection() as connection:
            project, _actor = _authorize_project(
                connection, auth_token, workspace_id, project_id, "project.update"
            )
            _check_project_version(project, expected_project_version)

    def find_matching_import(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        source_document_ids: list[str],
    ) -> PendingBriefVersion | None:
        """Return the latest version built from exactly the same frozen inputs."""
        if (
            not isinstance(source_document_ids, list)
            or not source_document_ids
            or len(source_document_ids) != len(set(source_document_ids))
            or not all(isinstance(item, str) and item for item in source_document_ids)
        ):
            raise WorkspaceServiceError("INVALID_INPUT", "正式 Brief 来源文件标识无效")
        expected = set(source_document_ids)
        with self.store.connection() as connection:
            _authorize_project(
                connection, auth_token, workspace_id, project_id, "project.read"
            )
            rows = connection.execute(
                """SELECT brief_version_id, source_document_ids_json
                   FROM brief_versions
                   WHERE workspace_id=? AND project_id=?
                   ORDER BY created_at DESC, rowid DESC""",
                (workspace_id, project_id),
            ).fetchall()
            for row in rows:
                ids = _json_load(
                    row["source_document_ids_json"], "source_document_ids_json"
                )
                if (
                    isinstance(ids, list)
                    and len(ids) == len(expected)
                    and set(ids) == expected
                ):
                    return _read_pending(
                        connection,
                        self.artifacts,
                        workspace_id,
                        project_id,
                        row["brief_version_id"],
                    )
        return None

    def latest_import_base(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
    ) -> PendingBriefVersion | None:
        """Return the latest immutable Brief version used as an append-only base."""
        with self.store.connection() as connection:
            _authorize_project(
                connection, auth_token, workspace_id, project_id, "project.read"
            )
            row = connection.execute(
                """SELECT brief_version_id FROM brief_versions
                   WHERE workspace_id=? AND project_id=?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (workspace_id, project_id),
            ).fetchone()
            if row is None:
                return None
            return _read_pending(
                connection, self.artifacts, workspace_id, project_id,
                row["brief_version_id"],
            )

    def list_imported_sources(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
    ) -> list[dict]:
        """List all successfully persisted project inputs for cumulative re-import."""
        with self.store.connection() as connection:
            _authorize_project(
                connection, auth_token, workspace_id, project_id, "project.read"
            )
            rows = connection.execute(
                """SELECT source_document_id, relative_path, filename, mime_type,
                          size_bytes, sha256, artifact_ref, critical
                   FROM project_files
                   WHERE workspace_id=? AND project_id=?
                   ORDER BY created_at, rowid""",
                (workspace_id, project_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def import_frozen(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        source_manifest: SourceManifest,
        source_artifacts: dict[str, ArtifactPayload],
        extracted: dict,
        expected_project_version: int,
        parent_brief_version_id: str | None = None,
        transaction_hook: Callable[[object, PendingBriefVersion, object], None] | None = None,
        field_update: str | None = None,
    ) -> PendingBriefVersion:
        _validate_source_artifacts(source_manifest, source_artifacts)
        checkpoint = self.artifacts.checkpoint()
        standard_checkpoint = self.creative_standard_artifacts.checkpoint()
        try:
            with self.store.transaction() as connection:
                project, actor = _authorize_project(connection, auth_token, workspace_id, project_id, "project.update")
                _check_project_version(project, expected_project_version)
                parent = None
                if parent_brief_version_id is not None:
                    parent = _read_pending(
                        connection, self.artifacts, workspace_id, project_id,
                        parent_brief_version_id,
                    )
                if field_update is not None:
                    latest = connection.execute("SELECT brief_version_id FROM brief_versions WHERE workspace_id=? AND project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (workspace_id, project_id)).fetchone()
                    if parent is None or latest is None or latest["brief_version_id"] != parent.brief_version_id:
                        raise WorkspaceServiceError("BRIEF_VERSION_CONFLICT", "需求已变化，请回读最新内容后保存")
                    version = self.domain.replaced_field_version(parent, source_manifest=source_manifest,
                        extracted=extracted, field_path=field_update, created_by_actor_id=actor["actor_id"])
                else:
                    version = self.domain.imported_version(
                        workspace_id=workspace_id, project_id=project_id,
                        source_manifest=source_manifest, extracted=extracted,
                        created_by_actor_id=actor["actor_id"],
                        parent_brief_version_id=parent_brief_version_id,
                        parent_version=parent,
                    )
                _persist_import(connection, version, source_artifacts, actor["actor_id"])
                standard = self.creative_standards.draft(
                    version, actor_id=actor["actor_id"], created_at=utc_now()
                )
                _persist_creative_standard(connection, standard)
                if transaction_hook is not None:
                    transaction_hook(connection, version, actor)
                _increment_project(connection, project_id, expected_project_version)
        except Exception:
            self.artifacts.rollback_to(checkpoint)
            self.creative_standard_artifacts.rollback_to(standard_checkpoint)
            raise
        return version

    def resolve_conflict(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        brief_version_id: str,
        conflict_id: str,
        selected_fact_id: str,
        expected_project_version: int,
    ) -> PendingBriefVersion:
        checkpoint = self.artifacts.checkpoint()
        standard_checkpoint = self.creative_standard_artifacts.checkpoint()
        try:
            with self.store.transaction() as connection:
                project, actor = _authorize_project(connection, auth_token, workspace_id, project_id, "project.update")
                _check_project_version(project, expected_project_version)
                version = _read_pending(connection, self.artifacts, workspace_id, project_id, brief_version_id)
                conflict = next((item for item in version.payload_data["conflicts"] if item.get("conflict_id") == conflict_id), None)
                if conflict is None or conflict.get("resolution_status") != "unresolved":
                    raise WorkspaceServiceError("NOT_FOUND", "待解决冲突不存在或已解决")
                if selected_fact_id not in conflict.get("fact_ids", []):
                    raise WorkspaceServiceError("INVALID_INPUT", "选择的事实不属于该冲突")
                fact = next((item for item in version.payload_data["facts"] if item.get("fact_id") == selected_fact_id), None)
                if fact is None:
                    raise WorkspaceServiceError("INTEGRITY_ERROR", "冲突关联事实不存在")
                resolved_at = utc_now()
                evidence = self.artifacts.write(_json({
                    "schema_version": "mplus-formal-resolution-evidence/v2",
                    "workspace_id": workspace_id,
                    "project_id": project_id,
                    "brief_version_id": brief_version_id,
                    "conflict_id": conflict_id,
                    "selected_fact_id": selected_fact_id,
                    "conflict_evidence_refs": conflict.get("evidence_refs", []),
                    "selected_fact_evidence_refs": fact.get("evidence_refs", []),
                    "selected_fact_value_sha256": sha256(
                        str(fact.get("value_json") or "").encode("utf-8")
                    ).hexdigest(),
                    "resolved_by_actor_id": actor["actor_id"],
                    "resolved_at": resolved_at,
                }).encode("utf-8"))
                resolved = self.domain.resolve_conflicts(
                    version,
                    resolutions={conflict_id: (selected_fact_id, evidence)},
                    actor_id=actor["actor_id"],
                    resolved_at=resolved_at,
                )
                _persist_derived_version(connection, resolved, actor["actor_id"])
                standard = self.creative_standards.draft(
                    resolved, actor_id=actor["actor_id"], created_at=utc_now()
                )
                _persist_creative_standard(connection, standard)
                _increment_project(connection, project_id, expected_project_version)
        except Exception:
            self.artifacts.rollback_to(checkpoint)
            self.creative_standard_artifacts.rollback_to(standard_checkpoint)
            raise
        return resolved

    def derive_template_resolution(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        parent_brief_version_id: str,
        removals: list[dict],
        resolution_artifact: ArtifactPayload,
        expected_project_version: int,
    ) -> PendingBriefVersion:
        """Create an append-only Brief version for confirmed template-wins choices."""
        checkpoint = self.artifacts.checkpoint()
        standard_checkpoint = self.creative_standard_artifacts.checkpoint()
        try:
            with self.store.transaction() as connection:
                project, actor = _authorize_project(
                    connection, auth_token, workspace_id, project_id, "project.update"
                )
                _check_project_version(project, expected_project_version)
                parent = _read_pending(
                    connection, self.artifacts, workspace_id, project_id,
                    parent_brief_version_id,
                )
                payload = self.domain._authoritative_payload(parent)
                facts = {str(item.get("fact_id") or ""): item for item in payload["facts"]}
                applied = []
                for removal in removals:
                    fact_id = str(removal.get("fact_id") or "")
                    text = str(removal.get("original_text") or "").strip()
                    fact = facts.get(fact_id)
                    if fact is None or not text:
                        raise WorkspaceServiceError(
                            "PLAN_CONFLICT_EVIDENCE_INVALID",
                            "模板优先决定无法绑定到原 Brief 事实",
                        )
                    try:
                        value = json.loads(str(fact["value_json"]))
                    except json.JSONDecodeError as exc:
                        raise WorkspaceServiceError(
                            "PLAN_CONFLICT_EVIDENCE_INVALID", "原 Brief 字段无法解析"
                        ) from exc
                    value = _remove_confirmed_clause(value, text)
                    fact["value_json"] = _json(value)
                    applied.append({
                        "conflict_id": str(removal.get("conflict_id") or ""),
                        "fact_id": fact_id,
                        "original_text": text,
                        "choice": "template_wins",
                    })
                payload["template_conflict_resolutions"] = [
                    *list(payload.get("template_conflict_resolutions") or []),
                    {
                        "schema_version": "mplus-plan-conflict-resolution/v1",
                        "artifact": resolution_artifact.as_mapping(),
                        "applied": applied,
                    },
                ]
                derived = self.domain._new_version(
                    workspace_id, project_id, parent.source_manifest, payload,
                    parent.brief_version_id, actor["actor_id"],
                    rekey_imported_entities=True,
                )
                _persist_derived_version(connection, derived, actor["actor_id"])
                standard = self.creative_standards.draft(
                    derived, actor_id=actor["actor_id"], created_at=utc_now()
                )
                _persist_creative_standard(connection, standard)
                _increment_project(connection, project_id, expected_project_version)
        except Exception:
            self.artifacts.rollback_to(checkpoint)
            self.creative_standard_artifacts.rollback_to(standard_checkpoint)
            raise
        return derived

    def confirm(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        brief_version_id: str,
        confirmed_at: str,
        expected_project_version: int,
    ) -> ConfirmedBriefInput:
        with self.store.transaction() as connection:
            project, actor = _authorize_project(connection, auth_token, workspace_id, project_id, "review.record")
            _check_project_version(project, expected_project_version)
            version = _read_pending(connection, self.artifacts, workspace_id, project_id, brief_version_id)
            existing = connection.execute("SELECT 1 FROM brief_confirmations WHERE brief_version_id=?", (brief_version_id,)).fetchone()
            if existing is not None:
                raise WorkspaceServiceError("ALREADY_CONFIRMED", "Brief version 已确认，不可重写")
            snapshot_id, confirmation_id = new_id("brief_snapshot"), new_id("brief_confirmation")
            # Formal Brief's own hard failures are authoritative prerequisites;
            # only a confirmable Brief proceeds to its paired standard card.
            confirmed = self.domain.confirm(
                version, actor_id=actor["actor_id"], confirmed_at=confirmed_at,
                binding=ConfirmationBinding(confirmation_id, snapshot_id),
            )
            routing = resolve_brief_category(version.payload_data)
            category_id = project["category_id"]
            if routing["status"] == "ready":
                category_id = routing["category_id"]
            elif category_id is None or routing["status"] == "needs_clarification":
                raise WorkspaceServiceError("BRIEF_CATEGORY_REQUIRED", routing["message"])
            standard = _read_creative_standard(
                connection, self.creative_standard_artifacts,
                workspace_id, project_id, brief_version_id,
            )
            self.creative_standards.require_confirmable(standard)
            standard_confirmation_id = new_id("creative_standard_confirmation")
            connection.execute(
                """INSERT INTO project_brief_snapshots(
                    brief_snapshot_id, project_id, brief_ref, brief_sha256, created_by_actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (snapshot_id, project_id, confirmed.payload.ref, confirmed.payload.sha256, actor["actor_id"], confirmed_at),
            )
            connection.execute(
                """INSERT INTO brief_confirmations(
                    confirmation_id, brief_version_id, brief_snapshot_id, workspace_id, project_id,
                    confirmation_status, unresolved_conflict_count, confirmed_by_actor_id, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, 'confirmed', 0, ?, ?)""",
                (confirmation_id, brief_version_id, snapshot_id, workspace_id, project_id, actor["actor_id"], confirmed_at),
            )
            connection.execute(
                """INSERT INTO creative_standard_confirmations(
                    creative_standard_confirmation_id, creative_standard_version_id,
                    brief_confirmation_id, brief_snapshot_id, workspace_id, project_id,
                    confirmed_by_actor_id, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    standard_confirmation_id, standard.creative_standard_version_id,
                    confirmation_id, snapshot_id, workspace_id, project_id,
                    actor["actor_id"], confirmed_at,
                ),
            )
            updated = connection.execute(
                """UPDATE projects SET current_brief_snapshot_id=?, current_creative_standard_version_id=?, category_id=?,
                    template_package_ref=CASE WHEN category_id IS ? THEN template_package_ref ELSE NULL END,
                    template_package_sha256=CASE WHEN category_id IS ? THEN template_package_sha256 ELSE NULL END,
                    lock_version=lock_version+1, updated_at=?
                WHERE project_id=? AND lock_version=?""",
                (snapshot_id, standard.creative_standard_version_id, category_id, category_id, category_id, utc_now(), project_id, expected_project_version),
            )
            if updated.rowcount != 1:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "project 在确认期间已变化")
        return confirmed

    def load_confirmed_creative_standard(
        self, value: ConfirmedCreativeStandardInput
    ) -> ConfirmedCreativeStandardInput:
        if not isinstance(value, ConfirmedCreativeStandardInput):
            raise TypeError("标准卡 authority 只接受 ConfirmedCreativeStandardInput")
        with self.store.connection() as connection:
            row = connection.execute(
                """SELECT c.*, s.brief_version_id, s.brief_payload_ref, s.brief_payload_sha256,
                          s.payload_ref, s.payload_sha256
                   FROM creative_standard_confirmations c
                   JOIN creative_standard_versions s ON s.creative_standard_version_id=c.creative_standard_version_id
                   WHERE c.creative_standard_confirmation_id=?""",
                (value.confirmation_id,),
            ).fetchone()
        if row is None:
            raise WorkspaceServiceError("NOT_FOUND", "正式创作标准卡确认记录不存在")
        stored = ConfirmedCreativeStandardInput(
            schema_version=CONFIRMED_CREATIVE_STANDARD_SCHEMA,
            workspace_id=row["workspace_id"], project_id=row["project_id"],
            creative_standard_version_id=row["creative_standard_version_id"],
            brief_version_id=row["brief_version_id"], confirmation_id=row["creative_standard_confirmation_id"],
            brief_snapshot_id=row["brief_snapshot_id"],
            brief_payload=ArtifactPayload(row["brief_payload_ref"], row["brief_payload_sha256"]),
            card_payload=ArtifactPayload(row["payload_ref"], row["payload_sha256"]),
            confirmation_status="confirmed", confirmed_by_actor_id=row["confirmed_by_actor_id"],
            confirmed_at=row["confirmed_at"],
        )
        if stored != value:
            raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", "标准卡确认记录与输入不一致")
        return stored

    def read_creative_standard_bytes(self, payload: ArtifactPayload) -> bytes:
        return self.creative_standard_artifacts.read(payload)

    def resolve_current_paired(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
    ) -> PairedConfirmedBriefCreativeStandardInput:
        with self.store.connection() as connection:
            project, _ = _authorize_project(
                connection, auth_token, workspace_id, project_id, "project.read"
            )
            lock_version = project["lock_version"]
            brief, standard = _current_confirmed_pair(connection, project, workspace_id)
        self.resolver().resolve_payload(brief)
        self.creative_standard_resolver().resolve(standard)
        with self.store.connection() as connection:
            project, _ = _authorize_project(
                connection, auth_token, workspace_id, project_id, "project.read"
            )
            if project["lock_version"] != lock_version:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目在标准卡读取期间已变化")
            rechecked_brief, rechecked_standard = _current_confirmed_pair(
                connection, project, workspace_id
            )
            if rechecked_brief != brief or rechecked_standard != standard:
                raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", "当前 Brief/标准卡绑定已变化")
        return PairedConfirmedBriefCreativeStandardInput(brief, standard)

    def creative_standard_status(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        creative_standard_version_id: str,
    ) -> str:
        with self.store.connection() as connection:
            project, _ = _authorize_project(
                connection, auth_token, workspace_id, project_id, "project.read"
            )
            row = connection.execute(
                """SELECT s.creative_standard_version_id, c.brief_snapshot_id
                   FROM creative_standard_versions s
                   LEFT JOIN creative_standard_confirmations c
                     ON c.creative_standard_version_id=s.creative_standard_version_id
                   WHERE s.creative_standard_version_id=? AND s.workspace_id=? AND s.project_id=?""",
                (creative_standard_version_id, workspace_id, project_id),
            ).fetchone()
            if row is None:
                other = connection.execute(
                    "SELECT 1 FROM creative_standard_versions WHERE creative_standard_version_id=?",
                    (creative_standard_version_id,),
                ).fetchone()
                if other is not None:
                    raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "标准卡不属于目标项目或工作区")
                raise WorkspaceServiceError("NOT_FOUND", "创作标准卡不存在")
            if row["brief_snapshot_id"] is None:
                return "draft"
            if (
                project["current_creative_standard_version_id"] == creative_standard_version_id
                and project["current_brief_snapshot_id"] == row["brief_snapshot_id"]
            ):
                return "confirmed"
            return "superseded"

    def resolve_confirmed(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        confirmed: ConfirmedBriefInput,
    ) -> dict:
        if not isinstance(confirmed, ConfirmedBriefInput):
            raise TypeError("正式 Brief 消费只接受 ConfirmedBriefInput")
        with self.store.connection() as connection:
            _authorize_project(connection, auth_token, workspace_id, project_id, "project.read")
        if (confirmed.workspace_id, confirmed.project_id) != (workspace_id, project_id):
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "确认 Brief 不属于目标项目或工作区")
        return self.resolver().resolve_payload(confirmed)

    def read_structured(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        brief_version_id: str,
    ) -> dict:
        """Read a pending or confirmed formal Brief for the HTTP/UI boundary."""
        with self.store.connection() as connection:
            _authorize_project(connection, auth_token, workspace_id, project_id, "project.read")
            version = _read_pending(connection, self.artifacts, workspace_id, project_id, brief_version_id)
            confirmation = connection.execute(
                "SELECT * FROM brief_confirmations WHERE brief_version_id=?", (brief_version_id,)
            ).fetchone()
            standard = _find_creative_standard(
                connection, workspace_id, project_id, brief_version_id,
            )
            if standard is None:
                creative_standard = {
                    "status": "unavailable",
                    "confirmed_at": None,
                    "reason": "requires_reimport",
                    "message": "请重新导入资料以生成标准卡",
                    "needs_clarification": True,
                }
            else:
                standard_content = self.creative_standards.read(standard)
                standard_confirmation = connection.execute(
                    """SELECT confirmed_at FROM creative_standard_confirmations
                       WHERE creative_standard_version_id=?""",
                    (standard.creative_standard_version_id,),
                ).fetchone()
                missing_fields = self.creative_standards.confirmation_gaps(
                    standard, content=standard_content,
                )
                creative_standard = {
                    "status": "confirmed" if standard_confirmation else "draft",
                    "confirmed_at": standard_confirmation["confirmed_at"] if standard_confirmation else None,
                    "content": standard_content,
                    "missing_fields": list(missing_fields),
                }
        return {
            "workspace_id": version.workspace_id,
            "project_id": version.project_id,
            "brief_version_id": version.brief_version_id,
            "parent_brief_version_id": version.parent_brief_version_id,
            "payload": version.payload.as_mapping(),
            "source_manifest": {
                "manifest": version.source_manifest.manifest.as_mapping(),
                "documents": [
                    {
                        "source_document_id": item.source_document_id,
                        "relative_path": item.relative_path,
                        "content_sha256": item.content_sha256,
                        "critical": item.critical,
                    }
                    for item in version.source_manifest.documents
                ],
            },
            "facts": version.payload_data["facts"],
            "conflicts": version.payload_data["conflicts"],
            "images": version.payload_data["images"],
            "extractions": version.payload_data["extractions"],
            "confirmation_status": confirmation["confirmation_status"] if confirmation else "pending",
            "creative_standard": creative_standard,
        }

    def read_current_marker(self, auth_token: str, *, workspace_id: str, project_id: str) -> str:
        """Authorized identity for caching a previously fully verified Brief read."""
        return self.read_current_formal(auth_token, workspace_id=workspace_id,
            project_id=project_id, _metadata_only=True)["marker"]

    def read_current_formal(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        _metadata_only: bool = False,
    ) -> dict:
        """Resolve the latest pending Brief or the current formal confirmation."""
        pending_version_id = None
        binding = None
        with self.store.connection() as connection:
            project, _ = _authorize_project(
                connection, auth_token, workspace_id, project_id, "project.read"
            )
            snapshot_id = project["current_brief_snapshot_id"]
            if snapshot_id is None:
                prior_confirmation = connection.execute(
                    "SELECT 1 FROM brief_confirmations WHERE workspace_id=? AND project_id=? LIMIT 1",
                    (workspace_id, project_id),
                ).fetchone()
                if prior_confirmation is not None:
                    raise WorkspaceServiceError(
                        "FORMAL_BRIEF_DRIFT", "当前项目资料与已确认 Brief 不一致。",
                    )
                pending_version_id = _latest_pending_version_id(
                    connection, workspace_id, project_id,
                )
                if pending_version_id is None:
                    return _current_brief_marker(connection, project, None, binding) if _metadata_only else {"status": "empty"}
                updated_at = project["updated_at"]
            else:
                binding = connection.execute(
                    """
                    SELECT s.brief_snapshot_id, s.project_id AS snapshot_project_id,
                           s.brief_ref AS snapshot_brief_ref,
                           s.brief_sha256 AS snapshot_brief_sha256,
                           c.brief_version_id, c.brief_snapshot_id AS confirmation_snapshot_id,
                           c.workspace_id AS confirmation_workspace_id,
                           c.project_id AS confirmation_project_id,
                           c.confirmation_status, c.unresolved_conflict_count,
                           v.workspace_id AS version_workspace_id, v.project_id AS version_project_id,
                           v.payload_ref, v.payload_sha256
                    FROM project_brief_snapshots s
                    LEFT JOIN brief_confirmations c ON c.brief_snapshot_id = s.brief_snapshot_id
                    LEFT JOIN brief_versions v ON v.brief_version_id = c.brief_version_id
                    WHERE s.brief_snapshot_id=? AND s.project_id=?
                    """,
                    (snapshot_id, project_id),
                ).fetchone()
                if binding is None:
                    raise WorkspaceServiceError(
                        "FORMAL_BRIEF_DRIFT", "当前项目资料关联不完整。",
                    )
                if binding["brief_version_id"] is None:
                    prior_confirmation = connection.execute(
                        "SELECT 1 FROM brief_confirmations WHERE workspace_id=? AND project_id=? LIMIT 1",
                        (workspace_id, project_id),
                    ).fetchone()
                    if prior_confirmation is not None:
                        raise WorkspaceServiceError(
                            "FORMAL_BRIEF_DRIFT", "当前项目资料与已确认 Brief 不一致。",
                        )
                    pending_version_id = _latest_pending_version_id(
                        connection, workspace_id, project_id,
                    )
                    if pending_version_id is None:
                        return _current_brief_marker(connection, project, None, binding) if _metadata_only else {"status": "empty"}
                    updated_at = project["updated_at"]
                else:
                    expected = {
                        "snapshot_project_id": project_id,
                        "confirmation_snapshot_id": snapshot_id,
                        "confirmation_workspace_id": workspace_id,
                        "confirmation_project_id": project_id,
                        "version_workspace_id": workspace_id,
                        "version_project_id": project_id,
                        "confirmation_status": "confirmed",
                        "unresolved_conflict_count": 0,
                    }
                    if any(binding[key] != value for key, value in expected.items()):
                        raise WorkspaceServiceError(
                            "FORMAL_BRIEF_DRIFT", "当前项目资料与已确认 Brief 不一致。",
                        )
                    version = None if _metadata_only else _read_pending(
                        connection, self.artifacts, workspace_id, project_id,
                        binding["brief_version_id"],
                    )
                    if (
                        (version is not None and (version.payload.ref != binding["payload_ref"]
                            or version.payload.sha256 != binding["payload_sha256"]))
                        or binding["snapshot_brief_ref"] != binding["payload_ref"]
                        or binding["snapshot_brief_sha256"] != binding["payload_sha256"]
                    ):
                        raise WorkspaceServiceError(
                            "FORMAL_BRIEF_DRIFT", "当前项目资料内容与已确认 Brief 不一致。",
                        )
                    pending_version_id = _latest_pending_descendant_id(
                        connection,
                        workspace_id,
                        project_id,
                        binding["brief_version_id"],
                    )
                    updated_at = project["updated_at"]
            if _metadata_only:
                return _current_brief_marker(connection, project,
                    pending_version_id or binding["brief_version_id"], binding)
        if pending_version_id is not None:
            pending = self.read_structured(
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                brief_version_id=pending_version_id,
            )
            extractions = pending["extractions"]
            conflicts = pending["conflicts"]
            return {
                "status": "ready",
                "brief_version_id": pending_version_id,
                "confirmation_status": "pending",
                "has_failed_extractions": any(
                    item.get("status") == "failed" for item in extractions
                ),
                "unresolved_conflict_count": sum(
                    item.get("resolution_status") != "resolved" for item in conflicts
                ),
                "updated_at": updated_at,
                "brief": pending,
                "creative_standard": pending["creative_standard"],
            }
        paired = self.resolve_current_paired(
            auth_token, workspace_id=workspace_id, project_id=project_id
        )
        standard_content = self.creative_standard_resolver().resolve(
            paired.creative_standard
        )
        extractions = version.payload_data["extractions"]
        conflicts = version.payload_data["conflicts"]
        return {
            "status": "ready",
            "brief_version_id": version.brief_version_id,
            "confirmation_status": "confirmed",
            "has_failed_extractions": any(item.get("status") == "failed" for item in extractions),
            "unresolved_conflict_count": sum(
                item.get("resolution_status") != "resolved" for item in conflicts
            ),
            "updated_at": updated_at,
            "brief": {
                "workspace_id": workspace_id,
                "project_id": project_id,
                "brief_version_id": version.brief_version_id,
                "source_manifest": {
                    "manifest": version.source_manifest.manifest.as_mapping(),
                    "documents": [
                        {
                            "source_document_id": item.source_document_id,
                            "relative_path": item.relative_path,
                            "content_sha256": item.content_sha256,
                            "critical": item.critical,
                        }
                        for item in version.source_manifest.documents
                    ],
                },
                "facts": version.payload_data["facts"],
                "conflicts": conflicts,
                "images": version.payload_data["images"],
                "extractions": extractions,
                "confirmation_status": "confirmed",
            },
            "creative_standard": {
                "status": paired.creative_standard.confirmation_status,
                "confirmed_at": paired.creative_standard.confirmed_at,
                "content": standard_content,
            },
        }


def _current_brief_marker(connection, project, version_id, binding):
    # Selection is owned by read_current_formal above, including pending descendants.
    # This fingerprint is only an invalidation hint, never an artifact validation.
    version = connection.execute("SELECT * FROM brief_versions WHERE brief_version_id=?",
        (version_id,)).fetchone() if version_id else None
    standards = connection.execute("""SELECT * FROM creative_standard_versions
        WHERE brief_version_id=? OR creative_standard_version_id=?
        ORDER BY creative_standard_version_id""",
        (version_id, project["current_creative_standard_version_id"])).fetchall()
    confirmations = []
    for standard in standards:
        confirmations.extend(dict(row) for row in connection.execute(
            "SELECT * FROM creative_standard_confirmations WHERE creative_standard_version_id=?",
            (standard["creative_standard_version_id"],)).fetchall())
    files = []
    if version:
        source_ids = _json_load(version["source_document_ids_json"], "source_document_ids_json")
        files = [dict(row) for row in connection.execute(
            "SELECT * FROM project_files WHERE project_id=? ORDER BY file_id",
            (project["project_id"],)).fetchall() if row["source_document_id"] in source_ids]
    value = {"project": dict(project), "binding": dict(binding) if binding else None, "files": files,
        "version": dict(version) if version else None,
        "standards": [dict(row) for row in standards], "confirmations": confirmations}
    return {"marker": sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()}


def _latest_pending_version_id(connection, workspace_id: str, project_id: str) -> str | None:
    row = connection.execute(
        """
        SELECT v.brief_version_id
        FROM brief_versions v
        LEFT JOIN brief_confirmations c ON c.brief_version_id=v.brief_version_id
        WHERE v.workspace_id=? AND v.project_id=? AND c.brief_version_id IS NULL
        ORDER BY v.created_at DESC, v.rowid DESC
        LIMIT 1
        """,
        (workspace_id, project_id),
    ).fetchone()
    return row["brief_version_id"] if row is not None else None


def _latest_pending_descendant_id(
    connection,
    workspace_id: str,
    project_id: str,
    confirmed_version_id: str,
) -> str | None:
    row = connection.execute(
        """
        WITH RECURSIVE descendants(brief_version_id) AS (
            SELECT brief_version_id FROM brief_versions
            WHERE workspace_id=? AND project_id=? AND parent_brief_version_id=?
            UNION ALL
            SELECT child.brief_version_id
            FROM brief_versions child
            JOIN descendants parent
              ON child.parent_brief_version_id=parent.brief_version_id
            WHERE child.workspace_id=? AND child.project_id=?
        )
        SELECT v.brief_version_id
        FROM descendants d
        JOIN brief_versions v ON v.brief_version_id=d.brief_version_id
        LEFT JOIN brief_confirmations c ON c.brief_version_id=v.brief_version_id
        WHERE c.brief_version_id IS NULL
        ORDER BY v.created_at DESC, v.rowid DESC
        LIMIT 1
        """,
        (
            workspace_id, project_id, confirmed_version_id,
            workspace_id, project_id,
        ),
    ).fetchone()
    return row["brief_version_id"] if row is not None else None


def _authorize_project(connection, auth_token: str, workspace_id: str, project_id: str, permission: str):
    project = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if project is None:
        raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
    if project["workspace_id"] != workspace_id:
        raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于目标 workspace")
    if permission != "project.read":
        require_active_project(project)
    return project, require_permission(connection, auth_token, workspace_id=workspace_id, project_id=project_id, permission=permission)


def _check_project_version(project, expected_project_version: int) -> None:
    if type(expected_project_version) is not int or expected_project_version < 0 or project["lock_version"] != expected_project_version:
        raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "project 在写入期间已变化")


def _increment_project(connection, project_id: str, expected_project_version: int) -> None:
    result = connection.execute(
        """UPDATE projects SET lock_version=lock_version+1, updated_at=?
        WHERE project_id=? AND lock_version=?""",
        (utc_now(), project_id, expected_project_version),
    )
    if result.rowcount != 1:
        raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "project 在写入期间已变化")


def _validate_source_artifacts(manifest: SourceManifest, source_artifacts: dict[str, ArtifactPayload]) -> None:
    if not isinstance(source_artifacts, dict) or set(source_artifacts) != {item.source_document_id for item in manifest.documents}:
        raise FormalBriefDomainError("每个冻结文件必须提供唯一 ArtifactRef/SHA")
    for item in manifest.documents:
        artifact = source_artifacts[item.source_document_id]
        if not isinstance(artifact, ArtifactPayload) or artifact.sha256 != item.content_sha256:
            raise FormalBriefDomainError("冻结文件 ArtifactRef/SHA 与 manifest 不一致")


def _persist_import(connection, version: PendingBriefVersion, source_artifacts: dict[str, ArtifactPayload], actor_id: str) -> None:
    sources = {item.get("document_id"): item for item in version.payload_data["source_documents"] if isinstance(item, dict)}
    if set(sources) != {item.source_document_id for item in version.source_manifest.documents}:
        raise FormalBriefDomainError("正式导入的来源文件集合无效")
    extractions = {item.get("document_id"): item for item in version.payload_data["extractions"]}
    file_ids = {}
    for frozen in version.source_manifest.documents:
        source = sources[frozen.source_document_id]
        if source.get("project_id") != version.project_id:
            raise FormalBriefDomainError("来源文件 project_id 与正式 Brief 不一致")
        size = source.get("size_bytes")
        if type(size) is not int or size < 0:
            raise FormalBriefDomainError("来源文件 size_bytes 无效")
        extraction = extractions[frozen.source_document_id]
        artifact = source_artifacts[frozen.source_document_id]
        existing_file = connection.execute(
            """SELECT * FROM project_files
               WHERE project_id=? AND sha256=?""",
            (version.project_id, frozen.content_sha256),
        ).fetchone()
        if existing_file is not None:
            expected_file = {
                "workspace_id": version.workspace_id,
                "source_document_id": frozen.source_document_id,
                "relative_path": frozen.relative_path,
                "filename": require_nonempty(source.get("filename"), "filename"),
                "mime_type": require_nonempty(source.get("mime_type"), "mime_type"),
                "size_bytes": size,
                "artifact_ref": artifact.ref,
                "critical": int(frozen.critical),
            }
            if any(existing_file[key] != value for key, value in expected_file.items()):
                raise WorkspaceServiceError(
                    "DUPLICATE_UPLOAD_CONFLICT",
                    "重复资料与已冻结记录不一致",
                )
            file_ids[frozen.source_document_id] = existing_file["file_id"]
            continue
        file_id = new_id("project_file")
        connection.execute(
            """INSERT INTO project_files(
                file_id, workspace_id, project_id, source_document_id, relative_path, filename, mime_type,
                size_bytes, sha256, artifact_ref, status, critical, created_by_actor_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (file_id, version.workspace_id, version.project_id, frozen.source_document_id,
             frozen.relative_path, require_nonempty(source.get("filename"), "filename"),
             require_nonempty(source.get("mime_type"), "mime_type"), size, frozen.content_sha256,
             artifact.ref, "failed" if extraction["status"] == "failed" else "extracted",
             int(frozen.critical), actor_id, utc_now()),
        )
        file_ids[frozen.source_document_id] = file_id
        extraction_id = new_id("document_extraction")
        error = extraction.get("error") or {}
        connection.execute(
            """INSERT INTO document_extractions(
                extraction_id, file_id, status, critical, error_code, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (extraction_id, file_id, extraction["status"], int(frozen.critical), error.get("code"), error.get("message"), utc_now()),
        )
        for location in extraction.get("locations", []):
            evidence = location["evidence"]
            connection.execute(
                """INSERT INTO document_locations(
                    location_id, extraction_id, source_document_id, artifact_ref, content_sha256, location_json, text_content
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (new_id("document_location"), extraction_id, frozen.source_document_id,
                 evidence["artifact_ref"], evidence["content_sha256"], _json(evidence["location"]), location["text"]),
            )
    for image in version.payload_data["images"]:
        for link in image["source_links"]:
            evidence = link["evidence"]
            existing_image = connection.execute(
                """SELECT 1 FROM document_images
                   WHERE file_id=? AND source_document_id=? AND image_sha256=?
                     AND artifact_ref=? AND content_sha256=?""",
                (
                    file_ids[link["source_document_id"]],
                    link["source_document_id"], image["content_sha256"],
                    evidence["artifact_ref"], evidence["content_sha256"],
                ),
            ).fetchone()
            if existing_image is not None:
                continue
            connection.execute(
                """INSERT INTO document_images(
                    document_image_id, file_id, source_document_id, image_sha256, artifact_ref, content_sha256, location_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (new_id("document_image"), file_ids[link["source_document_id"]], link["source_document_id"],
                 image["content_sha256"], evidence["artifact_ref"], evidence["content_sha256"], _json(evidence["location"])),
            )
    _persist_version_rows(connection, version, actor_id)


def _persist_derived_version(connection, version: PendingBriefVersion, actor_id: str) -> None:
    _persist_version_rows(connection, version, actor_id)


def _persist_creative_standard(connection, standard: CreativeStandardVersion) -> None:
    connection.execute(
        """INSERT INTO creative_standard_versions(
            creative_standard_version_id, workspace_id, project_id, brief_version_id,
            brief_payload_ref, brief_payload_sha256, payload_ref, payload_sha256,
            content_sha256, created_by_actor_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            standard.creative_standard_version_id, standard.workspace_id,
            standard.project_id, standard.brief_version_id, standard.brief_payload.ref,
            standard.brief_payload.sha256, standard.payload.ref, standard.payload.sha256,
            standard.content_sha256, standard.created_by_actor_id, standard.created_at,
        ),
    )


def _read_creative_standard(
    connection,
    artifacts: CreativeStandardArtifactStore,
    workspace_id: str,
    project_id: str,
    brief_version_id: str,
) -> CreativeStandardVersion:
    standard = _find_creative_standard(
        connection, workspace_id, project_id, brief_version_id,
    )
    if standard is not None:
        return standard
    other = connection.execute(
        "SELECT 1 FROM creative_standard_versions WHERE brief_version_id=?",
        (brief_version_id,),
    ).fetchone()
    if other is not None:
        raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "标准卡不属于目标项目或工作区")
    raise WorkspaceServiceError("CREATIVE_STANDARD_MISSING", "正式 Brief 缺少同版本创作标准卡")


def _find_creative_standard(
    connection,
    workspace_id: str,
    project_id: str,
    brief_version_id: str,
) -> CreativeStandardVersion | None:
    row = connection.execute(
        """SELECT * FROM creative_standard_versions
           WHERE workspace_id=? AND project_id=? AND brief_version_id=?""",
        (workspace_id, project_id, brief_version_id),
    ).fetchone()
    if row is None:
        return None
    return CreativeStandardVersion(
        workspace_id=row["workspace_id"], project_id=row["project_id"],
        creative_standard_version_id=row["creative_standard_version_id"],
        brief_version_id=row["brief_version_id"],
        brief_payload=ArtifactPayload(row["brief_payload_ref"], row["brief_payload_sha256"]),
        payload=ArtifactPayload(row["payload_ref"], row["payload_sha256"]),
        content_sha256=row["content_sha256"], content={},
        created_by_actor_id=row["created_by_actor_id"], created_at=row["created_at"],
    )


def _current_confirmed_pair(connection, project, workspace_id: str) -> tuple[ConfirmedBriefInput, ConfirmedCreativeStandardInput]:
    snapshot_id = project["current_brief_snapshot_id"]
    standard_id = project["current_creative_standard_version_id"]
    if snapshot_id is None and standard_id is None:
        raise WorkspaceServiceError("CREATIVE_STANDARD_MISSING", "项目尚无已确认的 Brief/创作标准卡")
    if snapshot_id is None or standard_id is None:
        raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", "当前 Brief 与创作标准卡指针不成对")
    row = connection.execute(
        """SELECT b.confirmation_id AS brief_confirmation_id, b.brief_version_id,
                  b.brief_snapshot_id, b.workspace_id AS brief_workspace_id,
                  b.project_id AS brief_project_id, b.confirmation_status,
                  b.unresolved_conflict_count, b.confirmed_by_actor_id AS brief_confirmed_by,
                  b.confirmed_at AS brief_confirmed_at, v.payload_ref AS brief_payload_ref,
                  v.payload_sha256 AS brief_payload_sha256,
                  c.creative_standard_confirmation_id, c.creative_standard_version_id,
                  c.brief_confirmation_id AS standard_brief_confirmation_id,
                  c.brief_snapshot_id AS standard_brief_snapshot_id,
                  c.workspace_id AS standard_workspace_id, c.project_id AS standard_project_id,
                  c.confirmed_by_actor_id AS standard_confirmed_by, c.confirmed_at AS standard_confirmed_at,
                  s.brief_version_id AS standard_brief_version_id,
                  s.brief_payload_ref AS standard_brief_payload_ref,
                  s.brief_payload_sha256 AS standard_brief_payload_sha256,
                  s.payload_ref AS standard_payload_ref, s.payload_sha256 AS standard_payload_sha256
           FROM brief_confirmations b
           JOIN brief_versions v ON v.brief_version_id=b.brief_version_id
           JOIN creative_standard_confirmations c ON c.brief_confirmation_id=b.confirmation_id
           JOIN creative_standard_versions s ON s.creative_standard_version_id=c.creative_standard_version_id
           WHERE b.brief_snapshot_id=? AND c.creative_standard_version_id=?""",
        (snapshot_id, standard_id),
    ).fetchone()
    if row is None or any(row[key] != value for key, value in {
        "brief_workspace_id": workspace_id, "brief_project_id": project["project_id"],
        "standard_workspace_id": workspace_id, "standard_project_id": project["project_id"],
        "standard_brief_confirmation_id": row["brief_confirmation_id"] if row else None,
        "standard_brief_snapshot_id": snapshot_id,
        "standard_brief_version_id": row["brief_version_id"] if row else None,
        "standard_brief_payload_ref": row["brief_payload_ref"] if row else None,
        "standard_brief_payload_sha256": row["brief_payload_sha256"] if row else None,
        "confirmation_status": "confirmed", "unresolved_conflict_count": 0,
    }.items()):
        raise WorkspaceServiceError("CREATIVE_STANDARD_DRIFT", "当前 Brief 与创作标准卡绑定不一致")
    brief = ConfirmedBriefInput(
        workspace_id=row["brief_workspace_id"], project_id=row["brief_project_id"],
        brief_version_id=row["brief_version_id"], confirmation_id=row["brief_confirmation_id"],
        brief_snapshot_id=row["brief_snapshot_id"],
        payload=ArtifactPayload(row["brief_payload_ref"], row["brief_payload_sha256"]),
        confirmation_status="confirmed", unresolved_conflict_count=0,
        confirmed_by_actor_id=row["brief_confirmed_by"], confirmed_at=row["brief_confirmed_at"],
    )
    standard = ConfirmedCreativeStandardInput(
        schema_version=CONFIRMED_CREATIVE_STANDARD_SCHEMA,
        workspace_id=row["standard_workspace_id"], project_id=row["standard_project_id"],
        creative_standard_version_id=row["creative_standard_version_id"],
        brief_version_id=row["standard_brief_version_id"],
        confirmation_id=row["creative_standard_confirmation_id"],
        brief_snapshot_id=row["standard_brief_snapshot_id"],
        brief_payload=ArtifactPayload(row["standard_brief_payload_ref"], row["standard_brief_payload_sha256"]),
        card_payload=ArtifactPayload(row["standard_payload_ref"], row["standard_payload_sha256"]),
        confirmation_status="confirmed", confirmed_by_actor_id=row["standard_confirmed_by"],
        confirmed_at=row["standard_confirmed_at"],
    )
    return brief, standard


def _persist_version_rows(connection, version: PendingBriefVersion, actor_id: str) -> None:
    connection.execute(
        """INSERT INTO brief_versions(
            brief_version_id, workspace_id, project_id, parent_brief_version_id, source_manifest_ref,
            source_manifest_sha256, source_document_ids_json, payload_ref, payload_sha256, created_by_actor_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (version.brief_version_id, version.workspace_id, version.project_id, version.parent_brief_version_id,
         version.source_manifest.manifest.ref, version.source_manifest.manifest.sha256,
         _json([item.source_document_id for item in version.source_manifest.documents]),
         version.payload.ref, version.payload.sha256, actor_id, utc_now()),
    )
    for fact in version.payload_data["facts"]:
        connection.execute(
            """INSERT INTO brief_facts(
                fact_id, brief_version_id, field_path, value_json, certainty, confidence, evidence_refs_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (fact["fact_id"], version.brief_version_id, fact["field_path"], fact["value_json"],
             fact["certainty"], fact["confidence"], _json(fact["evidence_refs"])),
        )
    for conflict in version.payload_data["conflicts"]:
        evidence = conflict.get("resolution_evidence")
        connection.execute(
            """INSERT INTO brief_conflicts(
                conflict_id, brief_version_id, field_path, fact_ids_json, evidence_refs_json, resolution_status,
                resolved_fact_id, resolved_by_actor_id, resolved_at, resolution_evidence_ref, resolution_evidence_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (conflict["conflict_id"], version.brief_version_id, conflict["field_path"],
             _json(conflict["fact_ids"]), _json(conflict["evidence_refs"]), conflict["resolution_status"],
             conflict.get("resolved_fact_id"), conflict.get("resolved_by_actor_id"), conflict.get("resolved_at"),
             evidence["ref"] if evidence else None, evidence["sha256"] if evidence else None),
        )


def _read_pending(connection, artifacts: FormalBriefArtifactStore, workspace_id: str, project_id: str, brief_version_id: str) -> PendingBriefVersion:
    row = connection.execute("SELECT * FROM brief_versions WHERE brief_version_id=? AND workspace_id=? AND project_id=?", (brief_version_id, workspace_id, project_id)).fetchone()
    if row is None:
        existing = connection.execute("SELECT 1 FROM brief_versions WHERE brief_version_id=?", (brief_version_id,)).fetchone()
        if existing is not None:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "正式 Brief version 不属于目标项目或工作区")
        raise WorkspaceServiceError("NOT_FOUND", "正式 Brief version 不存在或不属于项目")
    ids = _json_load(row["source_document_ids_json"], "source_document_ids_json")
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or not all(isinstance(item, str) and item for item in ids):
        raise WorkspaceServiceError("INTEGRITY_ERROR", "正式 Brief 来源文件关联已损坏")
    placeholders = ",".join("?" for _ in ids)
    files = connection.execute(
        f"SELECT * FROM project_files WHERE project_id=? AND source_document_id IN ({placeholders})",
        (project_id, *ids),
    ).fetchall()
    if len(files) != len(ids):
        raise WorkspaceServiceError("INTEGRITY_ERROR", "正式 Brief 来源文件不存在")
    by_id = {item["source_document_id"]: item for item in files}
    manifest = SourceManifest.from_mapping({"manifest": {"ref": row["source_manifest_ref"], "sha256": row["source_manifest_sha256"]}, "documents": [
        {"source_document_id": source_id, "relative_path": by_id[source_id]["relative_path"], "content_sha256": by_id[source_id]["sha256"], "critical": bool(by_id[source_id]["critical"])}
        for source_id in ids
    ]})
    payload = ArtifactPayload(row["payload_ref"], row["payload_sha256"])
    raw = artifacts.read(payload)
    envelope = _json_load(raw.decode("utf-8"), "formal Brief payload")
    if not isinstance(envelope, dict) or envelope.get("schema_version") != FORMAL_BRIEF_SCHEMA or any(envelope.get(key) != row[key] for key in ("workspace_id", "project_id", "brief_version_id")) or not isinstance(envelope.get("brief"), dict):
        raise WorkspaceServiceError("INTEGRITY_ERROR", "正式 Brief payload 与同库版本不一致")
    brief = envelope["brief"]
    return PendingBriefVersion(
        workspace_id, project_id, row["created_by_actor_id"], brief_version_id,
        row["parent_brief_version_id"], manifest, payload, brief,
        sum(item.get("resolution_status") == "unresolved" for item in brief.get("conflicts", [])),
        sum(item.get("status") == "failed" and manifest.critical_for(str(item.get("document_id") or "")) for item in brief.get("extractions", [])),
    )


def _remove_confirmed_clause(value, clause: str):
    if isinstance(value, str):
        parts = [
            item.strip()
            for item in value.replace("；", "\n").replace(";", "\n").splitlines()
            if item.strip() and item.strip() != clause
        ]
        if parts and len(parts) == 1 and parts[0] == value.strip() and clause in value:
            remainder = value.replace(clause, "").strip(" ，,、；;。\n")
            return remainder or "无特别要求"
        return "；".join(parts) if parts else "无特别要求"
    if isinstance(value, list):
        return [
            item for item in value
            if str(item.get("title") if isinstance(item, dict) else item).strip() != clause
        ]
    raise WorkspaceServiceError(
        "PLAN_CONFLICT_EVIDENCE_INVALID", "模板优先仅支持字符串或事件数组字段"
    )


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str, field: str):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkspaceServiceError("INTEGRITY_ERROR", f"{field} 无法解析") from exc


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
