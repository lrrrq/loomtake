from __future__ import annotations

from contextlib import nullcontext

import json

from .identity_service import require_permission
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    check_version,
    enqueue_outbox,
    new_id,
    require_artifact_ref,
    require_nonempty,
    row_dict,
    sha256_text,
    utc_now,
)


_SCOPES = {"company", "category", "project", "personal"}
_SCOPE_PRIORITY = {"company": 1, "category": 2, "project": 3, "personal": 4}


class KnowledgeService:
    """Workspace knowledge candidates and explicitly approved active rules."""

    def __init__(self, store: WorkspaceStore):
        self.store = store
        self.store.initialize()

    def get_state(
        self,
        auth_token: str,
        workspace_id: str,
        *,
        project_id: str | None = None,
    ) -> dict:
        with self.store.transaction() as connection:
            if project_id is not None:
                _project_context(connection, workspace_id, project_id)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                permission="project.read",
            )
            version = _ensure_state(connection, workspace_id)
        return {
            "workspace_id": workspace_id,
            "knowledge_version": version,
            "actor_id": actor["actor_id"],
        }

    def create_candidate(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        scope: str,
        target_id: str,
        statement: str,
        evidence_refs: list[str],
        expected_knowledge_version: int,
    ) -> dict:
        normalized_scope = _scope(scope)
        normalized_statement = require_nonempty(statement, "statement")
        normalized_evidence = _evidence_refs(evidence_refs)
        timestamp = utc_now()
        candidate_id = new_id("knowledge_candidate")
        with self.store.transaction() as connection:
            project = _project_context(connection, workspace_id, project_id)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                permission="knowledge.propose",
            )
            version = _ensure_state(connection, workspace_id)
            check_version(
                version,
                expected_knowledge_version,
                "KNOWLEDGE_VERSION_CONFLICT",
            )
            normalized_target = _target_for_scope(
                normalized_scope,
                target_id,
                workspace_id=workspace_id,
                project=project,
                actor_id=actor["actor_id"],
            )
            next_version = version + 1
            connection.execute(
                """
                INSERT INTO knowledge_candidates(
                    candidate_id, workspace_id, scope, target_id, statement,
                    statement_sha256, evidence_refs_json, proposed_by_actor_id,
                    knowledge_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    workspace_id,
                    normalized_scope,
                    normalized_target,
                    normalized_statement,
                    sha256_text(normalized_statement),
                    _json(normalized_evidence),
                    actor["actor_id"],
                    next_version,
                    timestamp,
                ),
            )
            _advance_state(connection, workspace_id, version, timestamp)
            enqueue_outbox(
                connection,
                aggregate_type="knowledge",
                aggregate_id=candidate_id,
                event_type="knowledge.candidate_created",
                payload={
                    "knowledge_version": next_version,
                    "scope": normalized_scope,
                    "target_id": normalized_target,
                },
            )
        return {
            "candidate_id": candidate_id,
            "workspace_id": workspace_id,
            "scope": normalized_scope,
            "target_id": normalized_target,
            "statement": normalized_statement,
            "statement_sha256": sha256_text(normalized_statement),
            "evidence_refs": normalized_evidence,
            "proposed_by_actor_id": actor["actor_id"],
            "knowledge_version": next_version,
            "status": "pending_approval",
            "created_at": timestamp,
        }

    def approve_candidate(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        candidate_id: str,
        level: str,
        review_evidence: str,
        expected_knowledge_version: int,
        _connection=None,
    ) -> dict:
        normalized_level = _scope(level)
        normalized_review = require_nonempty(review_evidence, "review_evidence")
        timestamp = utc_now()
        approval_id = new_id("knowledge_approval")
        rule_id = new_id("knowledge_rule")
        with (nullcontext(_connection) if _connection is not None else self.store.transaction()) as connection:
            project = _project_context(connection, workspace_id, project_id)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                permission="knowledge.approve",
            )
            version = _ensure_state(connection, workspace_id)
            check_version(
                version,
                expected_knowledge_version,
                "KNOWLEDGE_VERSION_CONFLICT",
            )
            candidate = connection.execute(
                "SELECT * FROM knowledge_candidates WHERE candidate_id = ?",
                (require_nonempty(candidate_id, "candidate_id"),),
            ).fetchone()
            if candidate is None:
                raise WorkspaceServiceError("NOT_FOUND", "知识候选不存在")
            if candidate["workspace_id"] != workspace_id:
                raise WorkspaceServiceError("PERMISSION_DENIED", "知识候选不属于当前工作区")
            if candidate["scope"] != normalized_level:
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED",
                    "批准层级必须与候选范围一致；跨层级推广必须重新建立候选并保留证据",
                )
            _target_for_scope(candidate["scope"], candidate["target_id"], workspace_id=workspace_id, project=project, actor_id=actor["actor_id"])
            if connection.execute(
                "SELECT 1 FROM knowledge_approvals WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone() is not None:
                raise WorkspaceServiceError("KNOWLEDGE_VERSION_CONFLICT", "知识候选已经批准")
            next_version = version + 1
            connection.execute(
                """
                INSERT INTO knowledge_approvals(
                    approval_id, candidate_id, approved_scope, review_evidence,
                    approved_by_actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    candidate_id,
                    normalized_level,
                    normalized_review,
                    actor["actor_id"],
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_rules(
                    rule_id, workspace_id, candidate_id, scope, target_id,
                    statement, statement_sha256, evidence_refs_json,
                    approved_by_actor_id, active_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    workspace_id,
                    candidate_id,
                    candidate["scope"],
                    candidate["target_id"],
                    candidate["statement"],
                    candidate["statement_sha256"],
                    candidate["evidence_refs_json"],
                    actor["actor_id"],
                    next_version,
                    timestamp,
                ),
            )
            _advance_state(connection, workspace_id, version, timestamp)
            enqueue_outbox(
                connection,
                aggregate_type="knowledge",
                aggregate_id=rule_id,
                event_type="knowledge.rule_approved",
                payload={
                    "candidate_id": candidate_id,
                    "knowledge_version": next_version,
                    "scope": normalized_level,
                    "target_id": candidate["target_id"],
                },
            )
        return {
            "approval_id": approval_id,
            "rule_id": rule_id,
            "candidate_id": candidate_id,
            "workspace_id": workspace_id,
            "scope": normalized_level,
            "target_id": candidate["target_id"],
            "statement": candidate["statement"],
            "evidence_refs": json.loads(candidate["evidence_refs_json"]),
            "approved_by_actor_id": actor["actor_id"],
            "knowledge_version": next_version,
            "status": "active",
            "created_at": timestamp,
        }

    def list_candidates(self, auth_token: str, workspace_id: str) -> list[dict]:
        with self.store.connection() as connection:
            require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                permission="project.read",
            )
            rows = connection.execute(
                """
                SELECT c.*, a.approval_id, r.rule_id, r.active_version
                FROM knowledge_candidates c
                LEFT JOIN knowledge_approvals a ON a.candidate_id = c.candidate_id
                LEFT JOIN knowledge_rules r ON r.candidate_id = c.candidate_id
                WHERE c.workspace_id = ?
                ORDER BY c.created_at, c.candidate_id
                """,
                (workspace_id,),
            ).fetchall()
        return [_candidate(row) for row in rows]

    def list_effective_rules(self, auth_token: str, project_id: str) -> dict:
        with self.store.connection() as connection:
            project = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (require_nonempty(project_id, "project_id"),),
            ).fetchone()
            if project is None:
                raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="project.read",
            )
            targets = {
                "company": project["workspace_id"],
                "category": project["category_id"],
                "project": project_id,
                "personal": actor["actor_id"],
            }
            rows = connection.execute(
                """
                SELECT * FROM knowledge_rules
                WHERE workspace_id = ? AND (
                    (scope = 'company' AND target_id = ?) OR
                    (scope = 'category' AND target_id = ?) OR
                    (scope = 'project' AND target_id = ?) OR
                    (scope = 'personal' AND target_id = ?)
                )
                ORDER BY active_version, rule_id
                """,
                (
                    project["workspace_id"],
                    targets["company"],
                    targets["category"],
                    targets["project"],
                    targets["personal"],
                ),
            ).fetchall()
            state = connection.execute(
                "SELECT lock_version FROM knowledge_states WHERE workspace_id = ?",
                (project["workspace_id"],),
            ).fetchone()
        rules = sorted(
            (_rule(row) for row in rows),
            key=lambda item: (_SCOPE_PRIORITY[item["scope"]], item["active_version"]),
        )
        return {
            "workspace_id": project["workspace_id"],
            "project_id": project_id,
            "category_id": project["category_id"],
            "actor_id": actor["actor_id"],
            "knowledge_version": int(state["lock_version"]) if state else 0,
            "rules": rules,
            "precedence": ["company", "category", "project", "personal"],
            "pending_candidates_included": False,
        }


def _ensure_state(connection, workspace_id: str) -> int:
    workspace = connection.execute(
        "SELECT workspace_id FROM workspaces WHERE workspace_id = ?",
        (require_nonempty(workspace_id, "workspace_id"),),
    ).fetchone()
    if workspace is None:
        raise WorkspaceServiceError("NOT_FOUND", "workspace 不存在")
    connection.execute(
        """
        INSERT OR IGNORE INTO knowledge_states(workspace_id, lock_version, updated_at)
        VALUES (?, 0, ?)
        """,
        (workspace_id, utc_now()),
    )
    row = connection.execute(
        "SELECT lock_version FROM knowledge_states WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    return int(row["lock_version"])


def _advance_state(connection, workspace_id: str, version: int, timestamp: str) -> None:
    updated = connection.execute(
        """
        UPDATE knowledge_states
        SET lock_version = lock_version + 1, updated_at = ?
        WHERE workspace_id = ? AND lock_version = ?
        """,
        (timestamp, workspace_id, version),
    )
    if updated.rowcount != 1:
        raise WorkspaceServiceError(
            "KNOWLEDGE_VERSION_CONFLICT", "知识版本在写入期间已变化"
        )


def _project_context(connection, workspace_id: str, project_id: str):
    project = connection.execute(
        "SELECT * FROM projects WHERE project_id = ?",
        (require_nonempty(project_id, "project_id"),),
    ).fetchone()
    if project is None:
        raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
    if project["workspace_id"] != workspace_id:
        raise WorkspaceServiceError("PERMISSION_DENIED", "project 不属于当前 workspace")
    return project


def _scope(value: str) -> str:
    normalized = require_nonempty(value, "scope")
    if normalized not in _SCOPES:
        raise WorkspaceServiceError("INVALID_INPUT", "知识范围不受支持")
    return normalized


def _target_for_scope(
    scope: str,
    target_id: str,
    *,
    workspace_id: str,
    project,
    actor_id: str,
) -> str:
    expected = {
        "company": workspace_id,
        "category": project["category_id"],
        "project": project["project_id"],
        "personal": actor_id,
    }[scope]
    if require_nonempty(target_id, "target_id") != expected:
        raise WorkspaceServiceError(
            "PERMISSION_DENIED", "知识 target_id 与当前身份、项目或类别范围不一致"
        )
    return expected


def _evidence_refs(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not values:
        raise WorkspaceServiceError("INVALID_INPUT", "evidence_refs 不能为空")
    normalized = [require_artifact_ref(value, "evidence_ref") for value in values]
    if len(normalized) > 100:
        raise WorkspaceServiceError("INVALID_INPUT", "evidence_refs 最多 100 条")
    if len(normalized) != len(set(normalized)):
        raise WorkspaceServiceError("INVALID_INPUT", "evidence_refs 不能重复")
    return normalized


def _candidate(row) -> dict:
    result = row_dict(row)
    return {
        "candidate_id": result["candidate_id"],
        "workspace_id": result["workspace_id"],
        "scope": result["scope"],
        "target_id": result["target_id"],
        "statement": result["statement"],
        "statement_sha256": result["statement_sha256"],
        "evidence_refs": json.loads(result["evidence_refs_json"]),
        "proposed_by_actor_id": result["proposed_by_actor_id"],
        "knowledge_version": result["knowledge_version"],
        "status": "active" if result.get("approval_id") else "pending_approval",
        "approval_id": result.get("approval_id"),
        "rule_id": result.get("rule_id"),
        "active_version": result.get("active_version"),
        "created_at": result["created_at"],
    }


def _rule(row) -> dict:
    result = row_dict(row)
    return {
        "rule_id": result["rule_id"],
        "candidate_id": result["candidate_id"],
        "scope": result["scope"],
        "target_id": result["target_id"],
        "statement": result["statement"],
        "statement_sha256": result["statement_sha256"],
        "evidence_refs": json.loads(result["evidence_refs_json"]),
        "approved_by_actor_id": result["approved_by_actor_id"],
        "active_version": result["active_version"],
        "created_at": result["created_at"],
    }


def _json(value: list[str]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
