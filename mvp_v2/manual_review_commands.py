"""Human-only T07 review commands. This module is intentionally not an Agent tool."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from .identity_service import require_permission
from .manual_version_commands import _audit, _claim_idempotency
from .review_evidence_artifacts import ReviewEvidenceArtifact, ReviewEvidenceAuthority
from .version_delivery import VersionDeliveryService
from .version_quality import AuthenticatedFinalConfirmationService, VersionQualityResolver
from .version_review_repository import WorkspaceVersionReviewRepository
from .version_review_service import VersionReviewError, VersionReviewService
from .workspace_store import WorkspaceServiceError, WorkspaceStore, new_id, utc_now


_ACTIONS = {
    "create_draft", "apply_change_set", "apply_agent_change_set", "delete_shot", "undo", "redo", "discard_changes", "set_lock_set",
    "create_review_items", "decide_review_items", "focus_recheck", "confirm_final",
}

_COMMON_PARAMETERS = {
    "project_id", "version_id", "expected_project_lock_version", "idempotency_key",
}
_ACTION_PARAMETERS = {
    "create_draft": {"shots"},
    "apply_change_set": {"draft_id", "expected_revision", "origin", "shot_no", "shot_id", "field", "value"},
    "apply_agent_change_set": {"draft_id", "expected_revision", "operations"},
    "delete_shot": {"draft_id", "expected_revision", "origin", "shot_no", "shot_id"},
    "undo": {"draft_id", "expected_revision"},
    "redo": {"draft_id", "expected_revision"},
    "discard_changes": {"draft_id", "expected_revision"},
    "set_lock_set": {"draft_id", "entries"},
    "create_review_items": {"shots", "items"},
    "decide_review_items": {"item_keys", "decision", "note"},
    "focus_recheck": {"item_keys", "note"},
    "confirm_final": {"decision", "note"},
}


@dataclass(frozen=True)
class ManualReviewCommandServices:
    store: WorkspaceStore
    delivery: VersionDeliveryService
    quality: VersionQualityResolver
    final_confirmation: AuthenticatedFinalConfirmationService
    evidence_authority: ReviewEvidenceAuthority


class ManualReviewCommandService:
    """Authenticated manual command boundary; zero Agent, publication and CURRENT paths."""

    def __init__(self, services: ManualReviewCommandServices):
        self.services = services
        self.services.store.initialize()

    def prepare(self, auth_token: str, *, action: str, parameters: dict[str, Any]) -> dict:
        normalized = self._validate(action, parameters)
        context = self._context(auth_token, normalized, action=action)
        command_id, nonce = new_id("manual_command"), secrets.token_urlsafe(24)
        digest = _digest(action, normalized)
        with self.services.store.transaction() as connection:
            connection.execute(
                "INSERT INTO manual_command_confirmations VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)",
                (command_id, context["actor_id"], context["project_id"], action, digest, _sha(nonce), utc_now()),
            )
            _claim_idempotency(connection, context, action, normalized["idempotency_key"], digest, command_id)
            _audit(connection, command_id, context, action, "prepared", digest)
        return {
            "command_token": nonce,
            "action": action, "risk": "R3", "channel": "manual_page",
            "scope": "仅处理人工审核业务记录",
        }

    def execute(
        self, auth_token: str, *, action: str, parameters: dict[str, Any],
        command_token: str, trusted_scope: str | None = None,
    ) -> dict:
        normalized = self._validate(action, parameters)
        context = self._context(auth_token, normalized, action=action)
        nonce = str(command_token or "").strip()
        if not nonce:
            raise WorkspaceServiceError("PERMISSION_DENIED", "人工审核命令确认令牌无效")
        digest = _digest(action, normalized)
        with self.services.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM manual_command_confirmations WHERE nonce_hash=?", (_sha(nonce),)
            ).fetchone()
            command_id = str(row["command_id"]) if row is not None else ""
            if row is None or row["status"] != "pending" or (
                row["actor_id"], row["project_id"], row["tool_name"],
                row["parameters_sha256"], row["nonce_hash"],
            ) != (context["actor_id"], context["project_id"], action, digest, _sha(nonce)):
                raise WorkspaceServiceError("PERMISSION_DENIED", "人工审核命令确认无效、已消费或不匹配")
            connection.execute(
                "UPDATE manual_command_confirmations SET status='consumed', consumed_at=? WHERE command_id=?",
                (utc_now(), command_id),
            )
            connection.execute(
                "UPDATE manual_command_idempotency SET status='consumed', updated_at=? WHERE command_id=?",
                (utc_now(), command_id),
            )
            _audit(connection, command_id, context, action, "executing", digest)
        try:
            result = self._execute_domain(
                auth_token, action, normalized, context, command_id=command_id,
                trusted_scope=trusted_scope,
            )
        except (WorkspaceServiceError, VersionReviewError) as exc:
            if not _is_recoverable_cas_conflict(exc):
                raise
            with self.services.store.transaction() as connection:
                evidence = self._domain_evidence(
                    connection, action=action, parameters=normalized, context=context,
                    command_id=command_id,
                )
                if evidence is None:
                    raise
                _audit_completed_once(connection, command_id, context, action, digest)
                return {"status": "completed", "result": evidence}
        with self.services.store.transaction() as connection:
            _audit_completed_once(connection, command_id, context, action, digest)
        return {"status": "completed", "result": result}

    def recover_idempotent(
        self, auth_token: str, *, action: str, parameters: dict[str, Any],
        trusted_scope: str | None = None,
    ) -> dict | None:
        """Resume a consumed/prepared command through the same domain owner.

        A command can be consumed before its domain transaction commits.  The
        existing idempotency row and audit trail are the recovery authority;
        callers must still verify the domain patch/checkpoint before claiming
        that the command completed.
        """
        if action not in {"create_draft", "apply_change_set"}:
            raise WorkspaceServiceError("IDEMPOTENCY_REPLAY_REQUIRED", "该人工命令不允许自动恢复")
        normalized = self._validate(action, parameters)
        context = self._context(auth_token, normalized, action=action)
        digest = _digest(action, normalized)
        with self.services.store.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM manual_command_idempotency
                WHERE actor_id=? AND project_id=? AND tool_name=? AND idempotency_key=?""",
                (context["actor_id"], context["project_id"], action, normalized["idempotency_key"]),
            ).fetchone()
            if row is None:
                return None
            if str(row["request_sha256"]) != digest:
                raise WorkspaceServiceError(
                    "IDEMPOTENCY_CONFLICT", "人工命令幂等键已用于不同内容",
                )
            completed = connection.execute(
                """SELECT 1 FROM manual_command_audits
                WHERE command_id=? AND tool_name=? AND status='completed' LIMIT 1""",
                (row["command_id"], action),
            ).fetchone()
            evidence = self._domain_evidence(
                connection, action=action, parameters=normalized, context=context,
                command_id=str(row["command_id"]),
            )
            if completed is not None:
                if evidence is None:
                    raise WorkspaceServiceError("INTEGRITY_ERROR", "已完成的人工命令缺少精确域证据")
                return evidence
            if evidence is not None:
                _audit_completed_once(connection, row["command_id"], context, action, digest)
                return evidence
            confirmation = connection.execute(
                "SELECT status FROM manual_command_confirmations WHERE command_id=?",
                (row["command_id"],),
            ).fetchone()
            if confirmation is None:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "人工命令确认记录缺失")
            if str(confirmation["status"]) == "pending":
                connection.execute(
                    "UPDATE manual_command_confirmations SET status='consumed', consumed_at=? WHERE command_id=?",
                    (utc_now(), row["command_id"]),
                )
                connection.execute(
                    "UPDATE manual_command_idempotency SET status='consumed', updated_at=? WHERE command_id=?",
                    (utc_now(), row["command_id"]),
                )
            _audit(connection, row["command_id"], context, action, "recovering", digest)
        try:
            result = self._execute_domain(
                auth_token, action, normalized, context, command_id=str(row["command_id"]),
                trusted_scope=trusted_scope,
            )
        except (WorkspaceServiceError, VersionReviewError) as exc:
            if not _is_recoverable_cas_conflict(exc):
                raise
            with self.services.store.transaction() as connection:
                evidence = self._domain_evidence(
                    connection, action=action, parameters=normalized, context=context,
                    command_id=str(row["command_id"]),
                )
                if evidence is None:
                    raise
                _audit_completed_once(connection, row["command_id"], context, action, digest)
                return evidence
        with self.services.store.transaction() as connection:
            evidence = self._domain_evidence(
                connection, action=action, parameters=normalized, context=context,
                command_id=str(row["command_id"]),
            )
            if evidence is None:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "命令恢复后缺少精确域证据")
            _audit_completed_once(connection, row["command_id"], context, action, digest)
        return evidence or result

    def read_model(
        self, auth_token: str, *, project_id: str, version_id: str,
        expected_project_lock_version: int, ancestor_of: str | None = None,
    ) -> dict:
        context = self._delivery_context(
            auth_token, project_id, version_id, expected_project_lock_version,
            ancestor_of=ancestor_of,
        )
        repository = self._repository(auth_token, context)
        return repository.read_models(version_id, evidence_authority=self.services.evidence_authority)

    def _execute_domain(
        self, auth_token: str, action: str, parameters: dict, context: dict,
        *, command_id: str | None = None, trusted_scope: str | None = None,
    ) -> dict:
        repository = self._repository(auth_token, context, trusted_scope=trusted_scope)
        review = VersionReviewService(repository)
        if action == "create_draft":
            self._register_subject(
                repository, parameters["shots"], context, permission="project.update",
            )
            draft = review.create_draft(
                workspace_id=context["workspace_id"], project_id=context["project_id"],
                base_version_id=context["version_id"], shots=parameters["shots"],
                actor_id=context["actor_id"], command_id=command_id,
            )
            return _draft_view(draft)
        if action == "apply_change_set":
            draft, patch = review.apply_table_patch(
                parameters["draft_id"], expected_revision=parameters["expected_revision"],
                actor_id=context["actor_id"], shot_no=parameters["shot_no"],
                field=parameters["field"], value=parameters["value"],
                command_id=command_id,
            ) if parameters["origin"] == "table" else review.apply_agent_patch(
                parameters["draft_id"], expected_revision=parameters["expected_revision"],
                actor_id=context["actor_id"], shot_no=parameters["shot_no"],
                field=parameters["field"], value=parameters["value"],
                command_id=command_id,
            )[:2]
            return {**_draft_view(draft), "patch_id": patch.patch_id, "origin": patch.origin}
        if action == "apply_agent_change_set":
            draft, patches = review.apply_agent_change_set(
                parameters["draft_id"],
                expected_revision=parameters["expected_revision"],
                actor_id=context["actor_id"],
                operations=parameters["operations"],
                command_id=command_id,
            )
            return {
                **_draft_view(draft),
                "patch_ids": [patch.patch_id for patch in patches],
                "origin": "natural_language",
            }
        if action == "delete_shot":
            draft, patch = review.delete_shot(
                parameters["draft_id"],
                expected_revision=parameters["expected_revision"],
                actor_id=context["actor_id"],
                shot_no=parameters["shot_no"],
                origin=parameters["origin"],
                command_id=command_id,
            )
            return {**_draft_view(draft), "patch_id": patch.patch_id, "origin": patch.origin}
        if action in {"undo", "redo", "discard_changes"}:
            draft = getattr(review, action)(
                parameters["draft_id"], expected_revision=parameters["expected_revision"],
                actor_id=context["actor_id"],
            )
            return _draft_view(draft)
        if action == "set_lock_set":
            lock_set = review.set_locks(
                parameters["draft_id"], entries=parameters["entries"], actor_id=context["actor_id"]
            )
            return {"lock_set_id": lock_set.lock_set_id, "entry_count": len(lock_set.entries)}
        if action == "create_review_items":
            self._register_subject(
                repository, parameters["shots"], context, permission="review.record",
            )
            def item_evidence(item_id, raw):
                artifact = self._materialize_evidence(
                    context, kind="review-item", note=raw["note"], review_item_id=item_id,
                )
                return artifact.ref, artifact.sha256, artifact.occurred_at
            items = review.add_review_items(
                parameters["version_id"], items=parameters["items"], evidence_provider=item_evidence,
            )
            return {"created_count": len(items), "issue_keys": [_issue_key(item.review_item_id) for item in items]}
        if action == "decide_review_items":
            self._verify_existing_evidence(repository, parameters["version_id"], context)
            decisions = []
            for item_id in self._item_ids(repository, parameters["version_id"], parameters["item_keys"]):
                artifact = self._materialize_evidence(
                    context, kind="human-decision", note=parameters["note"],
                    review_item_id=item_id, decision=parameters["decision"],
                )
                decisions.extend(review.decide_review_items(
                    parameters["version_id"], item_ids=[item_id], decision=parameters["decision"],
                    evidence_ref=artifact.ref, evidence_sha256=artifact.sha256,
                    reviewer_actor_id=context["actor_id"], decided_at=artifact.occurred_at,
                ))
            return {"decision_count": len(decisions)}
        if action == "focus_recheck":
            self._verify_existing_evidence(repository, parameters["version_id"], context)
            decisions = []
            for item_id in self._item_ids(repository, parameters["version_id"], parameters["item_keys"]):
                artifact = self._materialize_evidence(
                    context, kind="focus-recheck", note=parameters["note"], review_item_id=item_id,
                )
                decisions.extend(review.focus_recheck(
                    parameters["version_id"], item_ids=[item_id], evidence_ref=artifact.ref,
                    evidence_sha256=artifact.sha256, reviewer_actor_id=context["actor_id"],
                    decided_at=artifact.occurred_at,
                ))
            return {"decision_count": len(decisions)}
        self._verify_existing_evidence(repository, parameters["version_id"], context)
        snapshot = self.services.quality.materialize_shot_snapshot(
            auth_token, project_id=context["project_id"], version_id=context["version_id"],
            expected_project_lock_version=context["project_lock_version"],
        )
        artifact = self._materialize_evidence(
            context, kind="final-confirmation", note=parameters["note"], decision=parameters["decision"],
            snapshot_ref=snapshot.snapshot_ref, snapshot_sha256=snapshot.snapshot_sha256,
        )
        result = self.services.final_confirmation.confirm(
            auth_token, project_id=context["project_id"], version_id=context["version_id"],
            expected_project_lock_version=context["project_lock_version"],
            decision=parameters["decision"], evidence_ref=artifact.ref,
            evidence_sha256=artifact.sha256,
        )
        return {
            "decision": result.decision, "reviewer_label": "已由有权限人员处理",
            "confirmed_at": result.confirmed_at, "evidence_status": "available",
        }

    def _domain_evidence(
        self, connection, *, action: str, parameters: dict, context: dict,
        command_id: str,
    ) -> dict | None:
        """Read exact command-owned domain evidence for recovery/finalization."""
        if action == "create_draft":
            draft_id = _command_id(command_id, "draft")
            draft = connection.execute(
                """SELECT * FROM version_review_drafts
                WHERE draft_id=? AND workspace_id=? AND project_id=? AND base_version_id=?
                  AND created_by_actor_id=?""",
                (draft_id, context["workspace_id"], context["project_id"],
                 context["version_id"], context["actor_id"]),
            ).fetchone()
            state = connection.execute(
                """SELECT * FROM version_review_draft_states
                WHERE draft_id=? AND parent_state_id IS NULL""", (draft_id,),
            ).fetchone()
            head = connection.execute(
                "SELECT 1 FROM version_review_draft_heads WHERE draft_id=?", (draft_id,),
            ).fetchone()
            if draft is None or state is None or head is None:
                return None
            if str(state["snapshot_sha256"]) != _json_sha(parameters["shots"]):
                raise WorkspaceServiceError("INTEGRITY_ERROR", "创建草稿的初始快照与原命令不一致")
            return {"draft_id": draft_id, "revision": 0, "status": str(draft["status"])}
        if action != "apply_change_set":
            return None
        draft_id = str(parameters["draft_id"])
        patch_id = _command_id(command_id, "cell_patch")
        checkpoint_id = _command_id(command_id, "undo_checkpoint")
        draft = connection.execute(
            """SELECT * FROM version_review_drafts
            WHERE draft_id=? AND workspace_id=? AND project_id=? AND base_version_id=?""",
            (draft_id, context["workspace_id"], context["project_id"], context["version_id"]),
        ).fetchone()
        patch = connection.execute(
            "SELECT * FROM version_review_cell_patches WHERE patch_id=? AND draft_id=?",
            (patch_id, draft_id),
        ).fetchone()
        checkpoint = connection.execute(
            "SELECT * FROM version_review_undo_checkpoints WHERE checkpoint_id=? AND draft_id=?",
            (checkpoint_id, draft_id),
        ).fetchone()
        if draft is None or patch is None or checkpoint is None:
            return None
        if (
            patch["actor_id"] != context["actor_id"]
            or checkpoint["actor_id"] != context["actor_id"]
            or int(patch["revision"]) != int(parameters["expected_revision"]) + 1
            or int(checkpoint["revision"]) != int(patch["revision"])
            or str(patch["origin"]) != str(parameters["origin"])
            or str(patch["shot_no"]) != str(parameters["shot_no"])
            or str(patch["field_name"]) != str(parameters["field"])
            or json.loads(str(patch["new_value_json"])) != parameters["value"]
        ):
            raise WorkspaceServiceError("INTEGRITY_ERROR", "脚本命令的 patch/checkpoint 与原请求不一致")
        state = connection.execute(
            """SELECT 1 FROM version_review_draft_states
            WHERE draft_id=? AND snapshot_sha256=?""",
            (draft_id, checkpoint["snapshot_sha256"]),
        ).fetchone()
        if state is None:
            raise WorkspaceServiceError("INTEGRITY_ERROR", "脚本命令 checkpoint 没有对应不可变快照")
        return {
            "draft_id": draft_id, "revision": int(patch["revision"]),
            "applied_revision": int(patch["revision"]), "patch_id": patch_id,
            "origin": str(patch["origin"]), "status": str(draft["status"]),
        }

    def _materialize_evidence(self, context: dict, **values) -> ReviewEvidenceArtifact:
        artifact = self.services.evidence_authority.materialize(
            context, actor_id=context["actor_id"], **values,
        )
        self.services.evidence_authority.verify(
            context, artifact, actor_id=context["actor_id"], **values,
        )
        return artifact

    def _verify_existing_evidence(
        self, repository: WorkspaceVersionReviewRepository, version_id: str, context: dict,
    ) -> None:
        for item in repository.review_items(version_id):
            self.services.evidence_authority.verify_existing(
                context, ref=item.evidence_ref, sha256=item.evidence_sha256,
                kind="review-item", review_item_id=item.review_item_id,
                occurred_at=item.created_at,
            )
        for decision in repository.decisions(version_id):
            self.services.evidence_authority.verify_existing(
                context, ref=decision.evidence_ref, sha256=decision.evidence_sha256,
                kind="focus-recheck" if decision.decision == "recheck" else "human-decision",
                review_item_id=decision.review_item_id, decision=None if decision.decision == "recheck" else decision.decision,
                actor_id=decision.reviewer_actor_id, occurred_at=decision.decided_at,
            )

    @staticmethod
    def _item_ids(repository: WorkspaceVersionReviewRepository, version_id: str, item_keys: list[str]) -> list[str]:
        indexed = {_issue_key(item.review_item_id): item.review_item_id for item in repository.review_items(version_id)}
        if any(key not in indexed for key in item_keys):
            raise WorkspaceServiceError("NOT_FOUND", "审核问题 key 不属于当前 version")
        return [indexed[key] for key in item_keys]

    def _register_subject(
        self,
        repository: WorkspaceVersionReviewRepository,
        shots: list[dict],
        context: dict,
        *,
        permission: str,
    ) -> None:
        resolved = self.services.delivery.artifacts.resolve_stage_artifact(
            context["run_id"], "visual_binding",
            expected_manifest_sha256=context["manifest_sha256"],
        )
        try:
            payload = json.loads(resolved.path.read_text(encoding="utf-8"))
            rows = payload["script_rows"]
            expected = [str(row.get("shot_no", row.get("镜号", ""))).strip() for row in rows]
        except (OSError, UnicodeDecodeError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "visual_binding 无法提供审核镜头链") from exc
        actual = [str(row.get("shot_no") or "").strip() for row in shots]
        if not expected or actual != expected:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "审核草稿镜头链必须匹配当前 version 的 visual_binding")
        try:
            repository.formal_version(repository.version_id)
        except VersionReviewError:
            repository.register_subject(shots, permission=permission)

    def _context(self, auth_token: str, parameters: dict, *, action: str) -> dict:
        return self._delivery_context(
            auth_token, parameters["project_id"], parameters["version_id"],
            parameters["expected_project_lock_version"],
            permission=(
                "project.update"
                if action in {"create_draft", "apply_change_set", "apply_agent_change_set", "undo", "redo", "discard_changes", "set_lock_set"}
                else "review.record"
            ),
        )

    def _delivery_context(
        self,
        auth_token: str,
        project_id: str,
        version_id: str,
        expected: int,
        *,
        permission: str = "review.record",
        ancestor_of: str | None = None,
    ) -> dict:
        delivery = self.services.delivery.resolve(
            auth_token, project_id, version_id, expected_project_version=expected, ancestor_of=ancestor_of
        )
        with self.services.store.connection() as connection:
            actor = require_permission(
                connection, auth_token, workspace_id=delivery.workspace_id,
                project_id=project_id, permission=permission,
            )
        return {
            "actor_id": actor["actor_id"], "workspace_id": delivery.workspace_id,
            "project_id": delivery.project_id, "version_id": delivery.version_id,
            "run_id": delivery.run_id, "project_lock_version": delivery.project_lock_version,
            "manifest_sha256": delivery.manifest.sha256,
            "read_ancestor_of": ancestor_of,
        }

    def _repository(
        self, auth_token: str, context: dict, *, trusted_scope: str | None = None,
    ) -> WorkspaceVersionReviewRepository:
        return WorkspaceVersionReviewRepository(
            self.services.store, auth_token=auth_token, workspace_id=context["workspace_id"],
            project_id=context["project_id"], version_id=context["version_id"],
            run_id=context["run_id"], expected_project_lock_version=context["project_lock_version"],
            enforce_single_script_draft=trusted_scope == "formal_script",
            read_ancestor_of=context.get("read_ancestor_of"),
        )

    @staticmethod
    def _validate(action: str, raw: dict[str, Any]) -> dict:
        if action not in _ACTIONS or not isinstance(raw, dict):
            raise WorkspaceServiceError("INVALID_INPUT", "未知人工审核动作")
        required = {"project_id", "version_id", "expected_project_lock_version", "idempotency_key"}
        if not required <= set(raw):
            raise WorkspaceServiceError("INVALID_INPUT", "人工审核动作缺少上下文或幂等键")
        if _has_client_evidence_field(raw):
            raise WorkspaceServiceError("INVALID_INPUT", "人工审核命令不接受客户端证据引用、SHA 或 evidence key")
        if not set(raw) <= _COMMON_PARAMETERS | _ACTION_PARAMETERS[action]:
            raise WorkspaceServiceError("INVALID_INPUT", "人工审核命令包含未声明字段")
        value = dict(raw)
        if type(value["expected_project_lock_version"]) is not int or value["expected_project_lock_version"] < 0:
            raise WorkspaceServiceError("INVALID_INPUT", "expected_project_lock_version 无效")
        if not str(value["idempotency_key"]).strip():
            raise WorkspaceServiceError("INVALID_INPUT", "idempotency_key 不能为空")
        if action == "create_draft":
            value["shots"] = _require_shots(value.get("shots"))
        elif action in {"apply_change_set", "delete_shot"}:
            if value.get("origin") not in {"table", "natural_language"}:
                raise WorkspaceServiceError("INVALID_INPUT", "origin 必须是 table 或 natural_language")
            keys = ("draft_id", "shot_no", "field") if action == "apply_change_set" else ("draft_id", "shot_no")
            for key in keys:
                if not str(value.get(key) or "").strip():
                    raise WorkspaceServiceError("INVALID_INPUT", f"{key} 不能为空")
            _revision(value)
        elif action == "apply_agent_change_set":
            if not str(value.get("draft_id") or "").strip():
                raise WorkspaceServiceError("INVALID_INPUT", "draft_id 不能为空")
            _revision(value)
            operations = value.get("operations")
            if not isinstance(operations, list) or not operations:
                raise WorkspaceServiceError("INVALID_INPUT", "operations 不能为空")
            normalized = []
            for item in operations:
                if not isinstance(item, dict) or set(item) != {"shot_no", "field", "value"}:
                    raise WorkspaceServiceError("INVALID_INPUT", "批量修改包含未声明字段")
                if not str(item.get("shot_no") or "").strip() or not str(item.get("field") or "").strip():
                    raise WorkspaceServiceError("INVALID_INPUT", "批量修改目标不能为空")
                normalized.append({
                    "shot_no": str(item["shot_no"]),
                    "field": str(item["field"]),
                    "value": item.get("value"),
                })
            value["operations"] = normalized
        elif action in {"undo", "redo", "discard_changes"}:
            if not str(value.get("draft_id") or "").strip():
                raise WorkspaceServiceError("INVALID_INPUT", "draft_id 不能为空")
            _revision(value)
        elif action == "set_lock_set":
            if not str(value.get("draft_id") or "").strip() or not isinstance(value.get("entries"), list):
                raise WorkspaceServiceError("INVALID_INPUT", "锁定参数无效")
            value["entries"] = [tuple(item) for item in value["entries"] if isinstance(item, (list, tuple)) and len(item) == 2]
            if len(value["entries"]) != len(raw["entries"]):
                raise WorkspaceServiceError("INVALID_INPUT", "锁定条目无效")
        elif action == "create_review_items":
            value["shots"] = _require_shots(value.get("shots"))
            if not isinstance(value.get("items"), list) or not value["items"]:
                raise WorkspaceServiceError("INVALID_INPUT", "items 不能为空")
            for item in value["items"]:
                if not isinstance(item, dict):
                    raise WorkspaceServiceError("INVALID_INPUT", "审核问题必须是 object")
                if not set(item) <= {"kind", "shot_no", "severity", "note"}:
                    raise WorkspaceServiceError("INVALID_INPUT", "审核问题包含未声明字段")
                for key in ("kind", "severity", "note"):
                    if not isinstance(item.get(key), str) or not item[key].strip():
                        raise WorkspaceServiceError("INVALID_INPUT", f"审核问题 {key} 不能为空")
                if item.get("shot_no") is not None and not str(item["shot_no"]).strip():
                    raise WorkspaceServiceError("INVALID_INPUT", "审核问题 shot_no 无效")
        elif action in {"decide_review_items", "focus_recheck"}:
            if not isinstance(value.get("item_keys"), list) or not value["item_keys"] or not all(
                isinstance(key, str) and key.startswith("review_") for key in value["item_keys"]
            ):
                raise WorkspaceServiceError("INVALID_INPUT", "item_keys 不能为空或无效")
            _note(value)
            if action == "decide_review_items" and value.get("decision") not in {"approve", "return"}:
                raise WorkspaceServiceError("INVALID_INPUT", "decision 无效")
        else:
            _note(value)
            if value.get("decision") not in {"quality_pass", "quality_fail"}:
                raise WorkspaceServiceError("INVALID_INPUT", "最终 decision 无效")
        return value


def _require_shots(value: Any) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise WorkspaceServiceError("INVALID_INPUT", "shots 不能为空")
    return value


def _revision(value: dict) -> None:
    if type(value.get("expected_revision")) is not int or value["expected_revision"] < 0:
        raise WorkspaceServiceError("INVALID_INPUT", "expected_revision 无效")


def _note(value: dict) -> None:
    note = value.get("note")
    if not isinstance(note, str) or not note.strip() or len(note) > 4000:
        raise WorkspaceServiceError("INVALID_INPUT", "note 不能为空或过长")
    value["note"] = note.strip()


def _has_client_evidence_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"evidence_ref", "evidence_sha256", "evidence_key"}
            or _has_client_evidence_field(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_client_evidence_field(item) for item in value)
    return False


def _is_recoverable_cas_conflict(error: Exception) -> bool:
    if isinstance(error, WorkspaceServiceError):
        return error.code == "DRAFT_REVISION_CONFLICT"
    return isinstance(error, VersionReviewError) and "DRAFT_REVISION_CONFLICT" in str(error)


def _audit_completed_once(
    connection, command_id: str, context: dict, action: str, digest: str,
) -> None:
    existing = connection.execute(
        """SELECT 1 FROM manual_command_audits
        WHERE command_id=? AND tool_name=? AND status='completed' LIMIT 1""",
        (command_id, action),
    ).fetchone()
    if existing is None:
        _audit(connection, command_id, context, action, "completed", digest)


def _issue_key(review_item_id: str) -> str:
    return "review_" + hashlib.sha256(
        f"mplus-review-item-key/v1:{review_item_id}".encode("utf-8")
    ).hexdigest()[:32]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_sha(value: Any) -> str:
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _command_id(command_id: str, prefix: str) -> str:
    return f"{prefix}_{_sha(str(command_id))}"


def _digest(action: str, parameters: dict) -> str:
    return _sha(json.dumps({"action": action, "parameters": parameters}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _draft_view(draft) -> dict:
    return {
        "draft_id": draft.draft_id, "revision": draft.revision, "status": draft.status,
        "shot_count": len(draft.shots),
    }
