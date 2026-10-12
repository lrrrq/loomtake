from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from .artifact_service import ArtifactResolutionError, RunArtifactResolver
from .identity_service import require_permission
from .manual_actions import validate_manual_action_input
from .project_service import ProjectService
from .version_service import VersionService
from .version_publication_projection import VersionPublicationProjectionService
from .workspace_store import WorkspaceServiceError, WorkspaceStore, new_id, utc_now


_ACTIONS = {"record_version_review", "select_candidate_version", "publish_version"}


@dataclass(frozen=True)
class ManualVersionCommandServices:
    store: WorkspaceStore
    projects: ProjectService
    versions: VersionService
    artifacts: RunArtifactResolver
    publication_projection: VersionPublicationProjectionService | None = None


class ManualVersionCommandService:
    """Human-page R3 commands; intentionally independent from Agent sessions/tools."""

    def __init__(self, services: ManualVersionCommandServices):
        self.services = services
        self.services.store.initialize()

    def prepare(self, auth_token: str, *, tool_name: str, parameters: dict[str, Any]) -> dict:
        normalized = self._validate(tool_name, parameters)
        context = self._context(auth_token, normalized["project_id"], tool_name)
        command_id, nonce = new_id("manual_command"), secrets.token_urlsafe(24)
        digest = _sha256({"tool_name": tool_name, "parameters": normalized})
        with self.services.store.transaction() as connection:
            connection.execute("INSERT INTO manual_command_confirmations VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)",
                (command_id, context["actor_id"], normalized["project_id"], tool_name, digest, _sha256(nonce), utc_now()))
            _claim_idempotency(
                connection, context, tool_name, normalized["idempotency_key"], digest, command_id
            )
            _audit(connection, command_id, context, tool_name, "prepared", digest)
        return {"command_id": command_id, "command_token": f"{command_id}.{nonce}", "tool_name": tool_name,
                "parameters_sha256": digest, "risk": "R3", "channel": "manual_page",
                "will_not_change": "不会调用 Agent；只有交付证据通过投影校验后才更新 CURRENT。"}

    def execute(self, auth_token: str, *, tool_name: str, parameters: dict[str, Any], command_token: str) -> dict:
        normalized = self._validate(tool_name, parameters)
        context = self._context(auth_token, normalized["project_id"], tool_name)
        command_id, nonce = _token(command_token)
        digest = _sha256({"tool_name": tool_name, "parameters": normalized})
        with self.services.store.transaction() as connection:
            response = _read_confirmation_result(connection, command_id=command_id, nonce=nonce,
                context=context, tool_name=tool_name, digest=digest)
            if response is None:
                _consume_confirmation(connection, command_id, context, tool_name, digest)
                result = self._domain_action(auth_token, tool_name, normalized, connection=connection)
                response = {"command_id": command_id, "status": "completed", "result": result}
                _save_confirmation_result(connection, response, context, tool_name, digest)
        result = response["result"]
        if tool_name == "publish_version" and self.services.publication_projection is not None:
            projection = self.services.publication_projection.process_publication(
                result["publication_id"],
            )
            result = {
                **result,
                "projection_status": projection["projection_status"],
                "projection_error": projection.get("error_code", ""),
            }
        return {**response, "result": result}

    def _domain_action(self, auth_token: str, tool_name: str, parameters: dict, *, connection=None) -> dict:
        if tool_name == "record_version_review":
            evidence = self._review_evidence(auth_token, parameters["project_id"], parameters["version_id"])
            return self.services.versions.record_review(auth_token, version_id=parameters["version_id"], decision=evidence["decision"], evidence_ref=evidence["evidence_ref"], expected_project_version=parameters["expected_project_version"], _connection=connection)
        if tool_name == "select_candidate_version":
            return self.services.versions.select_candidate(auth_token, version_id=parameters["version_id"], expected_project_version=parameters["expected_project_version"], reason="manual_page_confirmed", _connection=connection)
        return self.services.versions.publish_version(auth_token, version_id=parameters["version_id"], review_id=parameters["review_id"], expected_project_version=parameters["expected_project_version"], _connection=connection)

    def _context(self, auth_token: str, project_id: str, tool_name: str) -> dict:
        permission = {"record_version_review": "review.record", "select_candidate_version": "version.select_candidate", "publish_version": "version.publish"}[tool_name]
        with self.services.store.connection() as connection:
            row = connection.execute("SELECT workspace_id FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
            actor = require_permission(connection, auth_token, workspace_id=row["workspace_id"], project_id=project_id, permission=permission)
        return {"actor_id": actor["actor_id"], "project_id": project_id}

    def _validate(self, tool_name: str, parameters: dict[str, Any]) -> dict:
        if tool_name not in _ACTIONS:
            raise WorkspaceServiceError("INVALID_INPUT", "未知人工版本命令")
        try:
            payload = validate_manual_action_input(tool_name, {**parameters, "confirmation_token": "manual-command-placeholder-000000"}).model_dump(mode="json")
        except (TypeError, ValueError) as exc:
            raise WorkspaceServiceError("INVALID_INPUT", "人工版本命令参数不符合合同") from exc
        payload.pop("confirmation_token", None)
        return payload

    def _review_evidence(self, auth_token: str, project_id: str, version_id: str) -> dict:
        version = next((item for item in self.services.projects.list_project_versions(auth_token, project_id) if item["version_id"] == version_id), None)
        if version is None:
            raise WorkspaceServiceError("NOT_FOUND", "version 不属于当前 project")
        binding = self.services.projects.get_run_binding(auth_token, version["run_id"])
        with self.services.store.connection() as connection:
            final = connection.execute(
                """SELECT decision, confirmation_ref
                FROM version_quality_final_confirmations
                WHERE project_id=? AND version_id=? AND run_id=?""",
                (project_id, version_id, version["run_id"]),
            ).fetchone()
        if final is not None:
            return {
                "decision": str(final["decision"]),
                "evidence_ref": str(final["confirmation_ref"]),
            }
        resolved = self.services.artifacts.resolve_artifact(
            version["run_id"], "run_report"
        )
        try:
            report = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactResolutionError("ARTIFACT_INVALID", "run_report 不可读取") from exc
        if report.get("run_id") != version["run_id"] or report.get("machine_audit_status") != "pass":
            raise WorkspaceServiceError("REVIEW_REQUIRED", "run_report 未通过机器审核或不属于目标 version")
        decision = str(report.get("human_review_status") or "")
        if decision not in {"quality_pass", "quality_fail"}:
            raise WorkspaceServiceError("REVIEW_REQUIRED", "人工审核结论尚未确认")
        self.services.artifacts.resolve_manifest(version["run_id"], expected_sha256=binding["manifest_sha256"])
        return {"decision": decision, "evidence_ref": f"artifact://run/{version['run_id']}/run_report"}


def _read_confirmation_result(connection, *, command_id, nonce, context, tool_name, digest):
    row = connection.execute("SELECT * FROM manual_command_confirmations WHERE command_id=?", (command_id,)).fetchone()
    if row is None or (row["actor_id"], row["project_id"], row["tool_name"], row["parameters_sha256"], row["nonce_hash"]) != (context["actor_id"], context["project_id"], tool_name, digest, _sha256(nonce)):
        raise WorkspaceServiceError("PERMISSION_DENIED", "人工命令确认无效或不匹配")
    receipt = connection.execute("SELECT response_json FROM manual_command_results WHERE command_id=?", (command_id,)).fetchone()
    if receipt:
        return json.loads(receipt["response_json"])
    if row["status"] != "pending":
        raise WorkspaceServiceError("MANUAL_RESULT_RECONCILIATION_REQUIRED", "原操作已消费但缺少结果回执，需要核对原操作记录；不能重复执行")
    return None


def _consume_confirmation(connection, command_id, context, tool_name, digest):
    connection.execute("UPDATE manual_command_confirmations SET status='consumed', consumed_at=? WHERE command_id=?", (utc_now(), command_id))
    connection.execute("UPDATE manual_command_idempotency SET status='consumed', updated_at=? WHERE command_id=?", (utc_now(), command_id))
    _audit(connection, command_id, context, tool_name, "executing", digest)


def _save_confirmation_result(connection, response, context, tool_name, digest):
    connection.execute("INSERT INTO manual_command_results(command_id,response_json) VALUES (?,?)",
        (response["command_id"], json.dumps(response, ensure_ascii=False, sort_keys=True)))
    _audit(connection, response["command_id"], context, tool_name, "completed", digest)


def _audit(connection, command_id: str, context: dict, tool_name: str, status: str, digest: str) -> None:
    connection.execute("INSERT INTO manual_command_audits VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (new_id("manual_audit"), command_id, context["actor_id"], context["project_id"], tool_name, status, digest, utc_now()))


def _claim_idempotency(connection, context: dict, tool_name: str, idempotency_key: str, digest: str, command_id: str) -> None:
    row = connection.execute(
        "SELECT request_sha256 FROM manual_command_idempotency WHERE actor_id=? AND project_id=? AND tool_name=? AND idempotency_key=?",
        (context["actor_id"], context["project_id"], tool_name, idempotency_key),
    ).fetchone()
    if row is not None:
        code = "IDEMPOTENCY_CONFLICT" if row["request_sha256"] != digest else "IDEMPOTENCY_REPLAY_REQUIRED"
        raise WorkspaceServiceError(code, "人工命令幂等键已使用，不能再签发新的执行令牌")
    timestamp = utc_now()
    connection.execute(
        "INSERT INTO manual_command_idempotency VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?)",
        (context["actor_id"], context["project_id"], tool_name, idempotency_key, digest, command_id, timestamp, timestamp),
    )


def record_atomic_manual_confirmation(connection, *, context: dict, action: str, idempotency_key: str, digest: str) -> None:
    """Receipt for an authenticated one-step confirmation in its domain transaction.

    The caller must verify the actor and commit this connection only together
    with the complete domain result. A no-op still reserves its operation key.
    This does not issue a token or enable another manual/Agent action.
    """
    existing = connection.execute("""SELECT request_sha256, status FROM manual_command_idempotency
        WHERE actor_id=? AND project_id=? AND tool_name=? AND idempotency_key=?""",
        (context["actor_id"], context["project_id"], action, idempotency_key)).fetchone()
    if existing is not None:
        if existing["request_sha256"] != digest:
            raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "同一操作标识已用于另一批确认，请重新核对")
        if existing["status"] != "consumed":
            raise WorkspaceServiceError("IDEMPOTENCY_REPLAY_REQUIRED", "原确认尚未完成，请查询原操作")
        return
    command_id, timestamp = new_id("manual_command"), utc_now()
    connection.execute("INSERT INTO manual_command_confirmations VALUES (?, ?, ?, ?, ?, ?, 'consumed', ?, ?)",
        (command_id, context["actor_id"], context["project_id"], action, digest,
         _sha256(secrets.token_urlsafe(24)), timestamp, timestamp))
    _claim_idempotency(connection, context, action, idempotency_key, digest, command_id)
    connection.execute("UPDATE manual_command_idempotency SET status='consumed', updated_at=? WHERE command_id=?", (timestamp, command_id))
    _audit(connection, command_id, context, action, "completed", digest)


def _sha256(value: Any) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _token(value: str) -> tuple[str, str]:
    parts = str(value or "").split(".", 1)
    if len(parts) != 2 or not all(parts):
        raise WorkspaceServiceError("PERMISSION_DENIED", "人工命令令牌无效")
    return parts[0], parts[1]
