from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy

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

    def add_project_convention(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        statement: str,
        evidence_refs: list[str],
        expected_knowledge_version: int,
        confirmed: bool,
    ) -> dict:
        """Save an explicit, shared project convention without changing the Brief.

        This project-only entry point uses the existing knowledge proposal
        permission. It cannot promote a candidate or approve company knowledge.
        """
        _require_convention_confirmation(confirmed)
        statement = require_nonempty(statement, "statement")
        evidence_refs = _evidence_refs(evidence_refs)
        convention_id = new_id("project_convention")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            actor, version = _convention_write_context(
                connection, auth_token, workspace_id, project_id,
                expected_knowledge_version,
            )
            rule_id = _write_convention_rule(
                connection, workspace_id=workspace_id, project_id=project_id,
                statement=statement, evidence_refs=evidence_refs,
                actor_id=actor["actor_id"], version=version + 1,
                timestamp=timestamp, reason="项目成员明确保存项目创作约定",
            )
            _append_convention_event(
                connection, convention_id=convention_id,
                workspace_id=workspace_id, project_id=project_id, rule_id=rule_id,
                event_type="created", actor_id=actor["actor_id"], version=version,
                reason="项目成员明确保存项目创作约定", timestamp=timestamp,
            )
            result = _convention(_find_convention(
                connection, workspace_id, project_id, convention_id,
            ))
        return result

    def correct_project_convention(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        convention_id: str,
        statement: str,
        evidence_refs: list[str],
        expected_knowledge_version: int,
        reason: str,
        confirmed: bool,
    ) -> dict:
        """Append a correction while preserving its original evidence and audit."""
        _require_convention_confirmation(confirmed)
        statement = require_nonempty(statement, "statement")
        evidence_refs = _evidence_refs(evidence_refs)
        reason = require_nonempty(reason, "reason")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            actor, version = _convention_write_context(
                connection, auth_token, workspace_id, project_id,
                expected_knowledge_version,
            )
            current = _find_convention(
                connection, workspace_id, project_id, convention_id,
            )
            if current["event_type"] in {"disabled", "deleted"}:
                raise WorkspaceServiceError(
                    "PROJECT_CONVENTION_INACTIVE",
                    "该项目约定已停止使用；如需采用新约定，请明确新增，原审计记录保留",
                )
            rule_id = _write_convention_rule(
                connection, workspace_id=workspace_id, project_id=project_id,
                statement=statement, evidence_refs=evidence_refs,
                actor_id=actor["actor_id"], version=version + 1,
                timestamp=timestamp, reason=reason,
            )
            _append_convention_event(
                connection, convention_id=convention_id,
                workspace_id=workspace_id, project_id=project_id, rule_id=rule_id,
                event_type="corrected", actor_id=actor["actor_id"], version=version,
                reason=reason, timestamp=timestamp,
            )
            result = _convention(_find_convention(
                connection, workspace_id, project_id, convention_id,
            ))
        return result

    def set_project_convention_status(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        convention_id: str,
        status: str,
        expected_knowledge_version: int,
        reason: str,
    ) -> dict:
        """Stop using a convention; deletion never erases immutable audit rows."""
        status = require_nonempty(status, "status")
        if status not in {"disabled", "deleted"}:
            raise WorkspaceServiceError(
                "INVALID_INPUT", "项目约定只能停用或从今后上下文删除",
            )
        reason = require_nonempty(reason, "reason")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            actor, version = _convention_write_context(
                connection, auth_token, workspace_id, project_id,
                expected_knowledge_version,
            )
            current = _find_convention(
                connection, workspace_id, project_id, convention_id,
            )
            if current["event_type"] == status:
                return {**_convention(current), "knowledge_version": version}
            if current["event_type"] == "deleted":
                raise WorkspaceServiceError(
                    "PROJECT_CONVENTION_INACTIVE",
                    "该约定已从今后上下文删除，原审计记录保留；需要时可明确新增约定",
                )
            _append_convention_event(
                connection, convention_id=convention_id,
                workspace_id=workspace_id, project_id=project_id,
                rule_id=current["rule_id"], event_type=status,
                actor_id=actor["actor_id"], version=version,
                reason=reason, timestamp=timestamp,
            )
            result = _convention(_find_convention(
                connection, workspace_id, project_id, convention_id,
            ))
        return result

    def list_project_conventions(
        self,
        auth_token: str,
        project_id: str,
        *,
        include_inactive: bool = False,
    ) -> dict:
        """Read current project conventions, optionally with retired statements.

        Retired text is an audit/invalidation input, never an effective rule.
        Corrected statements are included there even if the convention remains
        active, so a conversation summary cannot revive an earlier revision.
        """
        with self.store.connection() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            project = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (require_nonempty(project_id, "project_id"),),
            ).fetchone()
            if project is None:
                raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
            require_permission(
                connection, auth_token, workspace_id=project["workspace_id"],
                project_id=project_id, permission="project.read",
            )
            rows = _latest_conventions(connection, project["workspace_id"], project_id)
            state = connection.execute(
                "SELECT lock_version FROM knowledge_states WHERE workspace_id = ?",
                (project["workspace_id"],),
            ).fetchone()
            inactive = (
                _inactive_convention_statements(connection, project["workspace_id"], project_id)
                if include_inactive else None
            )
        conventions = [_convention(row) for row in rows]
        result = {
            "workspace_id": project["workspace_id"],
            "project_id": project_id,
            "knowledge_version": int(state["lock_version"]) if state else 0,
            "conventions": [
                item for item in conventions
                if include_inactive or item["status"] == "active"
            ],
        }
        if include_inactive:
            result["inactive_statements"] = inactive
        return result

    def project_convention_context(
        self, auth_token: str, project_id: str, *, context: dict,
    ) -> dict:
        """Project current convention lifecycle over a model's frozen evidence.

        This read view never edits the frozen version, Brief, company/category
        rules or the caller's context. Only explicitly retired convention rule
        identities are excluded; matching text is not an identity. Snapshot
        hashes continue to identify the original frozen evidence. Current
        conventions live separately so new/corrected project decisions do not
        masquerade as rules that were already present when the version froze.
        """
        knowledge = self.list_project_conventions(auth_token, project_id, include_inactive=True)
        retired = knowledge["inactive_statements"]
        retired_ids = {item["rule_id"] for item in retired if item.get("rule_id")}
        result = deepcopy(context)

        def retained(rule):
            return not (isinstance(rule, dict) and rule.get("rule_id") in retired_ids
                and rule.get("scope") in {None, "project"})

        for evidence in (result, result.get("frozen_basis")):
            if not isinstance(evidence, dict):
                continue
            effective = evidence.get("effective_knowledge")
            if isinstance(effective, dict) and isinstance(effective.get("rules"), list):
                effective["rules"] = [rule for rule in effective["rules"] if retained(rule)]
            elif isinstance(effective, list):
                evidence["effective_knowledge"] = [rule for rule in effective if retained(rule)]
        result.update(
            project_conventions=[item for item in knowledge["conventions"] if item["status"] == "active"],
            retired_conventions=retired,
            knowledge_version=knowledge["knowledge_version"],
        )
        return result

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
                SELECT c.*, a.approval_id, r.rule_id, r.active_version,
                       current.event_type AS convention_event_type,
                       current.rule_id AS current_convention_rule_id
                FROM knowledge_candidates c
                LEFT JOIN knowledge_approvals a ON a.candidate_id = c.candidate_id
                LEFT JOIN knowledge_rules r ON r.candidate_id = c.candidate_id
                LEFT JOIN knowledge_project_convention_events source
                  ON source.rule_id = r.rule_id
                 AND source.event_type IN ('created', 'corrected')
                LEFT JOIN knowledge_project_convention_events current
                  ON current.convention_id = source.convention_id
                 AND NOT EXISTS (
                     SELECT 1 FROM knowledge_project_convention_events newer
                     WHERE newer.convention_id = current.convention_id
                       AND newer.knowledge_version > current.knowledge_version
                 )
                WHERE c.workspace_id = ?
                ORDER BY c.created_at, c.candidate_id
                """,
                (workspace_id,),
            ).fetchall()
        return [_candidate(row) for row in rows]

    def list_effective_rules(self, auth_token: str, project_id: str) -> dict:
        with self.store.connection() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
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
                SELECT * FROM knowledge_rules r
                WHERE workspace_id = ? AND (
                    (scope = 'company' AND target_id = ?) OR
                    (scope = 'category' AND target_id = ?) OR
                    (scope = 'project' AND target_id = ?) OR
                    (scope = 'personal' AND target_id = ?)
                ) AND (
                    NOT EXISTS (
                        SELECT 1 FROM knowledge_project_convention_events e
                        WHERE e.rule_id = r.rule_id
                    ) OR EXISTS (
                        SELECT 1 FROM knowledge_project_convention_events e
                        WHERE e.rule_id = r.rule_id
                          AND e.event_type IN ('created', 'corrected')
                          AND NOT EXISTS (
                              SELECT 1 FROM knowledge_project_convention_events newer
                              WHERE newer.convention_id = e.convention_id
                                AND newer.knowledge_version > e.knowledge_version
                          )
                    )
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


def _require_convention_confirmation(confirmed: bool) -> None:
    if confirmed is not True:
        raise WorkspaceServiceError(
            "PROJECT_CONVENTION_CONFIRMATION_REQUIRED",
            "请明确确认要保存的项目创作约定；聊天意见不会自动成为项目要求",
        )


def _convention_write_context(
    connection, auth_token: str, workspace_id: str, project_id: str,
    expected_knowledge_version: int,
) -> tuple[dict, int]:
    _project_context(connection, workspace_id, project_id)
    actor = require_permission(
        connection, auth_token, workspace_id=workspace_id,
        project_id=project_id, permission="knowledge.propose",
    )
    version = _ensure_state(connection, workspace_id)
    check_version(version, expected_knowledge_version, "KNOWLEDGE_VERSION_CONFLICT")
    return actor, version


def _write_convention_rule(
    connection, *, workspace_id: str, project_id: str, statement: str,
    evidence_refs: list[str], actor_id: str, version: int, timestamp: str,
    reason: str,
) -> str:
    """Persist explicit project knowledge in the original immutable ledger."""
    candidate_id = new_id("knowledge_candidate")
    approval_id = new_id("knowledge_approval")
    rule_id = new_id("knowledge_rule")
    statement_sha256 = sha256_text(statement)
    evidence_json = _json(evidence_refs)
    connection.execute(
        """
        INSERT INTO knowledge_candidates(
            candidate_id, workspace_id, scope, target_id, statement,
            statement_sha256, evidence_refs_json, proposed_by_actor_id,
            knowledge_version, created_at
        ) VALUES (?, ?, 'project', ?, ?, ?, ?, ?, ?, ?)
        """,
        (candidate_id, workspace_id, project_id, statement, statement_sha256,
         evidence_json, actor_id, version, timestamp),
    )
    connection.execute(
        """
        INSERT INTO knowledge_approvals(
            approval_id, candidate_id, approved_scope, review_evidence,
            approved_by_actor_id, created_at
        ) VALUES (?, ?, 'project', ?, ?, ?)
        """,
        (approval_id, candidate_id, reason, actor_id, timestamp),
    )
    connection.execute(
        """
        INSERT INTO knowledge_rules(
            rule_id, workspace_id, candidate_id, scope, target_id,
            statement, statement_sha256, evidence_refs_json,
            approved_by_actor_id, active_version, created_at
        ) VALUES (?, ?, ?, 'project', ?, ?, ?, ?, ?, ?, ?)
        """,
        (rule_id, workspace_id, candidate_id, project_id, statement,
         statement_sha256, evidence_json, actor_id, version, timestamp),
    )
    return rule_id


def _append_convention_event(
    connection, *, convention_id: str, workspace_id: str, project_id: str,
    rule_id: str, event_type: str, actor_id: str, version: int, reason: str,
    timestamp: str,
) -> None:
    connection.execute(
        """
        INSERT INTO knowledge_project_convention_events(
            event_id, convention_id, workspace_id, project_id, rule_id,
            event_type, actor_id, knowledge_version, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id("project_convention_event"), convention_id, workspace_id,
         project_id, rule_id, event_type, actor_id, version + 1, reason, timestamp),
    )
    _advance_state(connection, workspace_id, version, timestamp)
    enqueue_outbox(
        connection, aggregate_type="knowledge", aggregate_id=convention_id,
        event_type=f"knowledge.project_convention_{event_type}",
        payload={
            "convention_id": convention_id, "project_id": project_id,
            "rule_id": rule_id, "scope": "project", "actor_id": actor_id,
            "knowledge_version": version + 1,
        },
    )


_CONVENTION_SELECT = """
    SELECT e.*, r.candidate_id, r.statement, r.statement_sha256,
           r.evidence_refs_json, r.approved_by_actor_id, r.active_version,
           (SELECT original.created_at
            FROM knowledge_project_convention_events original
            WHERE original.convention_id = e.convention_id
            ORDER BY original.knowledge_version LIMIT 1) AS initial_created_at
    FROM knowledge_project_convention_events e
    JOIN knowledge_rules r ON r.rule_id = e.rule_id
"""


def _find_convention(connection, workspace_id: str, project_id: str, convention_id: str):
    row = connection.execute(
        _CONVENTION_SELECT + """
        WHERE e.workspace_id = ? AND e.project_id = ? AND e.convention_id = ?
        ORDER BY e.knowledge_version DESC LIMIT 1
        """,
        (workspace_id, project_id, require_nonempty(convention_id, "convention_id")),
    ).fetchone()
    if row is None:
        raise WorkspaceServiceError("NOT_FOUND", "当前项目中不存在该项目约定")
    return row


def _latest_conventions(connection, workspace_id: str, project_id: str):
    return connection.execute(
        _CONVENTION_SELECT + """
        WHERE e.workspace_id = ? AND e.project_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM knowledge_project_convention_events newer
              WHERE newer.convention_id = e.convention_id
                AND newer.knowledge_version > e.knowledge_version
          )
        ORDER BY e.knowledge_version, e.convention_id
        """,
        (workspace_id, project_id),
    ).fetchall()


def _convention(row) -> dict:
    return {
        "convention_id": row["convention_id"],
        "workspace_id": row["workspace_id"],
        "project_id": row["project_id"],
        "scope": "project",
        "target_id": row["project_id"],
        "rule_id": row["rule_id"],
        "candidate_id": row["candidate_id"],
        "statement": row["statement"],
        "statement_sha256": row["statement_sha256"],
        "evidence_refs": json.loads(row["evidence_refs_json"]),
        "status": row["event_type"] if row["event_type"] in {"disabled", "deleted"} else "active",
        "actor_id": row["actor_id"],
        "approved_by_actor_id": row["approved_by_actor_id"],
        "active_version": row["active_version"],
        "knowledge_version": row["knowledge_version"],
        "revision_version": row["knowledge_version"],
        "reason": row["reason"],
        "created_at": row["initial_created_at"],
        "updated_at": row["created_at"],
        "audit_preserved": True,
    }


def _inactive_convention_statements(connection, workspace_id: str, project_id: str) -> list[dict]:
    rows = connection.execute(
        """
        SELECT e.convention_id, r.rule_id, r.statement, r.evidence_refs_json,
               r.active_version, current.event_type, current.rule_id AS current_rule_id,
               current.knowledge_version
        FROM knowledge_project_convention_events e
        JOIN knowledge_rules r ON r.rule_id = e.rule_id
        JOIN knowledge_project_convention_events current
          ON current.convention_id = e.convention_id
        WHERE e.workspace_id = ? AND e.project_id = ?
          AND e.event_type IN ('created', 'corrected')
          AND NOT EXISTS (
              SELECT 1 FROM knowledge_project_convention_events newer
              WHERE newer.convention_id = current.convention_id
                AND newer.knowledge_version > current.knowledge_version
          )
          AND (r.rule_id != current.rule_id OR current.event_type IN ('disabled', 'deleted'))
        ORDER BY e.knowledge_version, r.rule_id
        """,
        (workspace_id, project_id),
    ).fetchall()
    return [{
        "convention_id": row["convention_id"],
        "rule_id": row["rule_id"],
        "statement": row["statement"],
        "evidence_refs": json.loads(row["evidence_refs_json"]),
        "active_version": row["active_version"],
        "knowledge_version": row["knowledge_version"],
        "status": "superseded" if row["rule_id"] != row["current_rule_id"] else row["event_type"],
    } for row in rows]


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
    status = "active" if result.get("approval_id") else "pending_approval"
    if result.get("current_convention_rule_id"):
        if result["current_convention_rule_id"] != result.get("rule_id"):
            status = "superseded"
        elif result.get("convention_event_type") in {"disabled", "deleted"}:
            status = result["convention_event_type"]
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
        "status": status,
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
