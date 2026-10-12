from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any

from .manual_version_commands import ManualVersionCommandService
from .manual_asset_commands import ManualAssetPublishCommandService
from .asset_center import AssetCenterError
from .asset_center_tools import AssetCenterToolService
from .version_workbench import VersionWorkbenchService
from .workspace_store import WorkspaceServiceError


_ASSET_ACTIONS = {
    "create_asset_import",
    "review_asset_import",
    "publish_asset_import",
}
_VERSION_ACTIONS = {
    "record_version_review",
    "select_candidate_version",
    "publish_version",
}


@dataclass(frozen=True)
class UiWorkbenchServices:
    versions: VersionWorkbenchService
    assets: AssetCenterToolService
    manual_versions: ManualVersionCommandService | None = None
    manual_asset_publish: ManualAssetPublishCommandService | None = None


class UiWorkbenchService:
    """Thin authenticated adapter for the local version and asset desks."""

    def __init__(self, services: UiWorkbenchServices):
        self.services = services

    def version_tree(self, auth_token: str, project_id: str) -> dict:
        return self.services.versions.list_version_tree(auth_token, project_id)

    def read_version(
        self, auth_token: str, *, project_id: str, version_id: str,
    ) -> dict:
        return self.services.versions.read_version(
            auth_token, project_id=project_id, version_id=version_id,
        )

    def compare_versions(
        self,
        auth_token: str,
        *,
        project_id: str,
        left_version_id: str,
        right_version_id: str,
    ) -> dict:
        return self.services.versions.compare_versions(
            auth_token,
            project_id=project_id,
            left_version_id=left_version_id,
            right_version_id=right_version_id,
        )

    def prepare_version_action(
        self,
        auth_token: str,
        *,
        tool_name: str,
        parameters: dict[str, Any],
    ) -> dict:
        if tool_name not in _VERSION_ACTIONS:
            raise WorkspaceServiceError("INVALID_INPUT", "未知版本治理动作")
        confirmation = self._manual_versions().prepare(auth_token, tool_name=tool_name, parameters=parameters)
        changes = {
            "record_version_review": "登记 run_report 中已验证的人工审核结论",
            "select_candidate_version": "把目标 version 设为当前项目候选",
            "publish_version": "创建唯一正式发布登记并进入投影等待队列",
        }
        return {
            **confirmation,
            "action": tool_name,
            "risk": "R3",
            "will_change": changes[tool_name],
            "will_not_change": (
                "不会调用模型，不会修改运行产物；发布投影只在交付证据有效时更新 CURRENT"
            ),
            "calls_models": False,
            "impact_scope": f"当前项目 {parameters.get('project_id', '')} 的目标 version",
        }

    def execute_version_action(
        self,
        auth_token: str,
        *,
        tool_name: str,
        parameters: dict[str, Any],
        command_token: str,
    ) -> dict:
        if tool_name not in _VERSION_ACTIONS:
            raise WorkspaceServiceError("INVALID_INPUT", "未知版本治理动作")
        return self._manual_versions().execute(auth_token, tool_name=tool_name, parameters=parameters, command_token=command_token)

    def _manual_versions(self) -> ManualVersionCommandService:
        if self.services.manual_versions is None:
            raise WorkspaceServiceError("NOT_FOUND", "人工版本命令服务未配置")
        return self.services.manual_versions

    def prepare_asset_publish(
        self, auth_token: str, *, project_id: str, parameters: dict[str, Any]
    ) -> dict:
        if self.services.manual_asset_publish is None:
            raise WorkspaceServiceError("NOT_FOUND", "人工资产发布命令服务未配置")
        return self.services.manual_asset_publish.prepare(
            auth_token, project_id=project_id, parameters=parameters
        )

    def execute_asset_publish(
        self, auth_token: str, *, project_id: str, parameters: dict[str, Any], command_token: str
    ) -> dict:
        if self.services.manual_asset_publish is None:
            raise WorkspaceServiceError("NOT_FOUND", "人工资产发布命令服务未配置")
        return self.services.manual_asset_publish.execute(
            auth_token, project_id=project_id, parameters=parameters, command_token=command_token
        )

    def asset_context(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
    ) -> dict:
        session = self.services.assets._session(
            auth_token,
            agent_session_id=agent_session_id,
            permission="asset.read",
        )
        return self.asset_project_context(auth_token, project_id=session["project_id"])

    def asset_project_context(self, auth_token: str, *, project_id: str) -> dict:
        from .identity_service import require_permission
        project = self.services.assets.services.projects.get_project(auth_token, project_id)
        with self.services.assets.services.store.connection() as connection:
            require_permission(connection, auth_token, workspace_id=project["workspace_id"], project_id=project_id, permission="asset.read")
        package = self.services.assets.services.packages.resolve_active(
            project["category_id"]
        )
        return {
            "project_id": project["project_id"],
            "category_id": package.category_id,
            "package_id": package.package_id,
            "package_version": package.package_version,
            "imports": _visible_imports(
                package.center,
                category_id=package.category_id,
                project_id=project["project_id"],
            ),
            "provider_calls": 0,
        }

    def freeze_asset(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        source: str,
    ) -> dict:
        self.services.assets._session(
            auth_token,
            agent_session_id=agent_session_id,
            permission="asset.import",
        )
        return self.services.assets.freeze_upload(source)

    def freeze_uploaded_asset(self, auth_token: str, *, agent_session_id: str,
                              filename: str, stream) -> dict:
        self.services.assets._session(auth_token, agent_session_id=agent_session_id,
                                      permission="asset.import")
        return self.services.assets.services.uploads.freeze_stream(filename, stream)

    def prepare_asset_action(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
    ) -> dict:
        if tool_name not in _ASSET_ACTIONS:
            raise WorkspaceServiceError("INVALID_INPUT", "未知资产中心动作")
        confirmation = self.services.assets.issue_confirmation(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=parameters,
        )
        return {
            **confirmation,
            "action": tool_name,
            "risk": "R3" if tool_name == "publish_asset_import" else "R2",
            "calls_models": False,
            "will_not_change": "不会更新 CURRENT，不会覆盖现有资产包版本，也不会调用视觉 Provider",
        }

    def execute_asset_action(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        confirmation_token: str,
    ) -> dict:
        if tool_name == "create_asset_import":
            return self.services.assets.create_asset_import(
                auth_token,
                agent_session_id=agent_session_id,
                parameters=parameters,
                confirmation_token=confirmation_token,
            )
        if tool_name == "review_asset_import":
            return self.services.assets.review_asset_import(
                auth_token,
                agent_session_id=agent_session_id,
                parameters=parameters,
                confirmation_token=confirmation_token,
            )
        if tool_name == "publish_asset_import":
            return self.services.assets.publish_asset_import(
                auth_token,
                agent_session_id=agent_session_id,
                parameters={
                    **parameters,
                    "confirmation_token": confirmation_token,
                },
            )
        raise WorkspaceServiceError("INVALID_INPUT", "未知资产中心动作")

    def search_assets(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        category_id: str,
        query: str,
        kind: str,
    ) -> dict:
        return self.services.assets.search_assets(
            auth_token,
            agent_session_id=agent_session_id,
            parameters={
                "category_id": category_id,
                "query": query,
                "filters": {"kind": kind, "limit": 40},
            },
        )


def _visible_imports(center, *, category_id: str, project_id: str) -> list[dict]:
    if not center.catalog_path.exists():
        return []
    try:
        with sqlite3.connect(
            f"file:{center.catalog_path}?mode=ro", uri=True
        ) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_imports'"
            ).fetchone()
            if exists is None:
                return []
            rows = connection.execute(
                """
                SELECT import_id FROM asset_imports
                WHERE category_id=?
                  AND (scope='shared' OR target_project_id=?)
                ORDER BY updated_at DESC, import_id DESC
                """,
                (category_id, project_id),
            ).fetchall()
    except sqlite3.Error as exc:
        raise WorkspaceServiceError(
            "INTERNAL_ERROR", "资产导入记录暂时不可读取"
        ) from exc
    imports = []
    for row in rows:
        try:
            imports.append(center.get_import(str(row[0])))
        except AssetCenterError as exc:
            raise WorkspaceServiceError(
                "INTERNAL_ERROR", "资产导入记录完整性校验失败"
            ) from exc
    return imports
