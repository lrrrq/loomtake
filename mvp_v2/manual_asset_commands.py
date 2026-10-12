from __future__ import annotations

from dataclasses import dataclass
import secrets
import json
from typing import Any

from .asset_center import AssetCenterError
from .asset_center_tools import AssetPackageResolver
from .identity_service import require_permission
from .manual_actions import validate_manual_action_input
from .manual_version_commands import (_audit, _claim_idempotency, _sha256, _token,
    _consume_confirmation, _save_confirmation_result)
from .project_service import ProjectService
from .workspace_store import WorkspaceServiceError, WorkspaceStore, new_id, utc_now


@dataclass(frozen=True)
class ManualAssetPublishCommandServices:
    store: WorkspaceStore
    projects: ProjectService
    packages: AssetPackageResolver


class ManualAssetPublishCommandService:
    """Human-only E05 publication command; it never accepts an Agent session."""

    tool_name = "publish_asset_import"

    def __init__(self, services: ManualAssetPublishCommandServices):
        self.services = services
        self.services.store.initialize()

    def prepare(self, auth_token: str, *, project_id: str, parameters: dict[str, Any]) -> dict:
        normalized = self._validate(parameters)
        context = self._context(auth_token, project_id, normalized)
        command_id, nonce = new_id("manual_command"), secrets.token_urlsafe(24)
        digest = _sha256({"tool_name": self.tool_name, "project_id": project_id, "parameters": normalized})
        with self.services.store.transaction() as connection:
            connection.execute(
                "INSERT INTO manual_command_confirmations VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)",
                (command_id, context["actor_id"], project_id, self.tool_name, digest, _sha256(nonce), utc_now()),
            )
            _claim_idempotency(connection, context, self.tool_name, normalized["idempotency_key"], digest, command_id)
            _audit(connection, command_id, context, self.tool_name, "prepared", digest)
        record = context["record"]
        return {"command_id": command_id, "command_token": f"{command_id}.{nonce}", "risk": "R3",
            "channel": "manual_page", "will_not_change": "不会调用 Agent、不会写入 CURRENT。",
            "target_summary": {"import_id": record["import_id"], "scope": record["scope"],
                "category_id": record["category_id"], "target_project_id": record["target_project_id"],
                "items": [{key: item[key] for key in ("filename", "asset_kind", "content_sha256")}
                    for item in record["items"]]}}


    def execute(self, auth_token: str, *, project_id: str, parameters: dict[str, Any], command_token: str) -> dict:
        normalized = self._validate(parameters)
        context = self._authorized_context(auth_token, project_id)
        command_id, nonce = _token(command_token)
        digest = _sha256({"tool_name": self.tool_name, "project_id": project_id, "parameters": normalized})
        with self.services.store.connection() as connection:
            row, receipt = self._confirmation(connection, command_id, nonce, context, digest)
            if receipt is not None:
                return receipt
            consumed = row["status"] != "pending"
        context = self._context(auth_token, project_id, normalized, check_current=False)
        command = {"command_id": command_id, "request_sha256": digest, "actor_id": context["actor_id"],
            "workspace_id": context["workspace_id"], "project_id": project_id,
            "expected_revision": normalized["expected_import_revision"]}
        center = context["package"].center
        try:
            prepared = center.publication_stage(normalized["import_id"], command, "prepared")
            if prepared is None:
                if consumed:
                    raise WorkspaceServiceError("MANUAL_RESULT_RECONCILIATION_REQUIRED",
                        "原资产发布已消费但缺少持久关联，需要核对；不能重新发布")
                self._context(auth_token, project_id, normalized)
                center.bind_publication_command(normalized["import_id"], command)
            with self.services.store.transaction() as connection:
                require_permission(connection, auth_token, workspace_id=context["workspace_id"],
                    project_id=project_id, permission="asset.publish")
                row, receipt = self._confirmation(connection, command_id, nonce, context, digest)
                if receipt is not None:
                    return receipt
                if row["status"] == "pending":
                    _consume_confirmation(connection, command_id, context, self.tool_name, digest)
            result = center.publish_import(normalized["import_id"], published_by_actor_id=context["actor_id"],
                expected_revision=normalized["expected_import_revision"], publication_command=command)
        except AssetCenterError as exc:
            with self.services.store.transaction() as connection:
                _, receipt = self._confirmation(connection, command_id, nonce, context, digest)
                if receipt is not None:
                    return receipt
                _audit(connection, command_id, context, self.tool_name, "failed", digest)
            raise WorkspaceServiceError(exc.code, str(exc)) from exc
        response = {"command_id": command_id, "status": "completed", "result": result}
        with self.services.store.transaction() as connection:
            _, receipt = self._confirmation(connection, command_id, nonce, context, digest)
            if receipt is not None:
                return receipt
            _save_confirmation_result(connection, response, context, self.tool_name, digest)
        return response

    def _confirmation(self, connection, command_id, nonce, context, digest):
        row = connection.execute("SELECT * FROM manual_command_confirmations WHERE command_id=?", (command_id,)).fetchone()
        if row is None or (row["actor_id"], row["project_id"], row["tool_name"], row["parameters_sha256"], row["nonce_hash"]) != (
                context["actor_id"], context["project_id"], self.tool_name, digest, _sha256(nonce)):
            raise WorkspaceServiceError("PERMISSION_DENIED", "人工命令确认无效或不匹配")
        receipt = connection.execute("SELECT response_json FROM manual_command_results WHERE command_id=?", (command_id,)).fetchone()
        return row, json.loads(receipt["response_json"]) if receipt else None

    def _authorized_context(self, auth_token, project_id):
        project = self.services.projects.get_project(auth_token, project_id)
        with self.services.store.connection() as connection:
            actor = require_permission(connection, auth_token, workspace_id=project["workspace_id"],
                project_id=project_id, permission="asset.publish")
        return {"actor_id": actor["actor_id"], "project_id": project_id, "workspace_id": project["workspace_id"]}

    def _validate(self, parameters: dict[str, Any]) -> dict:
        try:
            payload = validate_manual_action_input(self.tool_name, {**parameters, "confirmation_token": "manual-command-placeholder-000000"}).model_dump(mode="json")
        except (TypeError, ValueError) as exc:
            raise WorkspaceServiceError("INVALID_INPUT", "人工资产发布参数不符合合同") from exc
        payload.pop("confirmation_token", None)
        return payload

    def _context(self, auth_token: str, project_id: str, parameters: dict, *, check_current=True) -> dict:
        project = self.services.projects.get_project(auth_token, project_id)
        with self.services.store.connection() as connection:
            actor = require_permission(connection, auth_token, workspace_id=project["workspace_id"], project_id=project_id, permission="asset.publish")
        try:
            package = self.services.packages.resolve(parameters["package_id"])
        except WorkspaceServiceError as exc:
            if check_current or exc.code != "NOT_FOUND":
                raise
            package = self.services.packages.resolve_active(project["category_id"])
        if check_current and (package.category_id != project["category_id"] or package.package_version != parameters["expected_active_package_version"]):
            raise WorkspaceServiceError("PACKAGE_VERSION_CONFLICT", "资产包不属于当前人工项目或版本已变化")
        try:
            record = package.center.get_import(parameters["import_id"])
        except AssetCenterError as exc:
            raise WorkspaceServiceError("NOT_FOUND", "资产导入记录不存在") from exc
        if ((check_current and record["category_id"] != project["category_id"])
                or record["package_id"] != parameters["package_id"]
                or (record["scope"] == "project" and record["target_project_id"] != project_id)):
            raise WorkspaceServiceError("PERMISSION_DENIED", "资产导入记录不属于当前人工项目")
        return {"actor_id": actor["actor_id"], "project_id": project_id,
            "workspace_id": project["workspace_id"], "package": package, "record": record}
