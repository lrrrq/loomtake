from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable
from urllib.parse import quote, unquote

from pydantic import ValidationError

from .action_confirmation import ActionConfirmationService, ManualPageConfirmationService
from .agent_tools import validate_tool_input
from .manual_actions import validate_manual_action_input
from .asset_center import AssetCenterError, AssetCenterService, AssetImportFile
from .asset_package import AssetPackageError, load_asset_package
from .asset_storage import AssetStorageError, LocalAssetStorage
from .excel_io import IMAGE_EXTENSIONS
from .identity_service import require_permission
from .project_service import ProjectService
from .runtime_config import load_runtime_config
from .task_service import TaskService
from .tool_idempotency import ToolIdempotencyService, ManualPageIdempotencyService
from .workspace_store import WorkspaceServiceError, WorkspaceStore


_UPLOAD_REF = re.compile(
    r"^artifact://upload/(?P<sha>[0-9a-f]{64})/(?P<name>[A-Za-z0-9._~!$&'()*+,;=:@%-]+)$"
)
_IMPORT_TOOLS = {
    "create_asset_import",
    "review_asset_import",
    "publish_asset_import",
}


class AssetToolRecoveryRequired(RuntimeError):
    code = "RECOVERY_RECONCILIATION_REQUIRED"

    def __init__(self, *, tool_name: str, idempotency_record_id: str):
        self.tool_name = tool_name
        self.idempotency_record_id = idempotency_record_id
        self.retryable = False
        self.recovery = {
            "automatic_retry": False,
            "reason": "领域写入与幂等结果分属不同事务，无法安全判断动作是否已经生效",
            "required_checks": [
                "核对对应 asset_import、asset_import_events 与 task_events",
                "由管理员人工补记幂等结果，或确认未写入后使用新幂等键",
            ],
        }
        super().__init__(
            f"{self.code}: {tool_name} 结果不确定，已阻断自动重放"
        )


class AssetUploadStore:
    """Freeze local upload bytes without retaining the source filesystem path."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        storage: LocalAssetStorage | None = None,
    ):
        root = Path(data_root).expanduser().resolve() / "artifacts" / "assets"
        self.storage = storage or LocalAssetStorage(root)
        if self.storage.root != root:
            raise ValueError("upload storage 必须位于 data_root/artifacts/assets")

    def freeze(self, source: str | Path) -> dict:
        path = Path(source).expanduser()
        if not path.is_file():
            raise WorkspaceServiceError("NOT_FOUND", "待导入文件不存在")
        filename = path.name
        kind = _asset_kind(filename)
        try:
            with path.open("rb") as handle:
                storage_ref, digest = self.storage.put(handle)
        except OSError as exc:
            raise WorkspaceServiceError("INVALID_INPUT", "无法读取待导入文件") from exc
        safe_name = quote(filename, safe="A-Za-z0-9._~!$&'()*+,;=:@-")
        return {
            "artifact_ref": f"artifact://upload/{digest}/{safe_name}",
            "storage_ref": storage_ref,
            "content_sha256": digest,
            "basename": filename,
            "kind": kind,
        }

    def freeze_stream(self, filename: str, stream) -> dict:
        """Validate browser bytes before placing them in the shared content store."""
        from io import BytesIO
        from .asset_center import _validate_image, _validate_xlsx
        from .input_limits import FORMAL_UPLOAD_FILE_BYTES

        if not filename or "/" in filename or "\\" in filename or Path(filename).name != filename:
            raise WorkspaceServiceError("INVALID_INPUT", "上传文件名无效")
        kind = _asset_kind(filename)
        content = stream.read(FORMAL_UPLOAD_FILE_BYTES + 1)
        if len(content) > FORMAL_UPLOAD_FILE_BYTES:
            raise WorkspaceServiceError("FILE_TOO_LARGE", "单文件不得超过5 MiB")
        try:
            if kind == "image":
                info = _validate_image(content)
                suffix = Path(filename).suffix.casefold().lstrip(".")
                expected = {"jpg": "jpeg", "tif": "tiff"}.get(suffix, suffix)
                if info["format"] != expected:
                    raise AssetCenterError("IMAGE_INVALID", "文件扩展名与实际图片格式不符")
            else:
                _validate_xlsx(content)
        except AssetCenterError as exc:
            raise WorkspaceServiceError("INVALID_INPUT", str(exc)) from exc
        storage_ref, digest = self.storage.put(BytesIO(content))
        safe_name = quote(filename, safe="A-Za-z0-9._~!$&'()*+,;=:@-")
        return {"artifact_ref": f"artifact://upload/{digest}/{safe_name}",
                "storage_ref": storage_ref, "content_sha256": digest,
                "basename": filename, "kind": kind}

    def resolve(self, artifact_ref: str) -> dict:
        value = str(artifact_ref or "").strip()
        match = _UPLOAD_REF.fullmatch(value)
        if match is None:
            raise WorkspaceServiceError("INVALID_INPUT", "file_ref 不是受控 upload 引用")
        digest = match.group("sha")
        encoded_name = match.group("name")
        try:
            filename = unquote(encoded_name, errors="strict")
        except UnicodeDecodeError as exc:
            raise WorkspaceServiceError("INVALID_INPUT", "upload 文件名编码无效") from exc
        if (
            not filename
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or quote(filename, safe="A-Za-z0-9._~!$&'()*+,;=:@-") != encoded_name
        ):
            raise WorkspaceServiceError("INVALID_INPUT", "upload 文件名无效")
        kind = _asset_kind(filename)
        storage_ref = f"sha256://{digest}"
        try:
            self.storage.verify(storage_ref, digest)
        except AssetStorageError as exc:
            raise WorkspaceServiceError(
                "INVALID_INPUT", "upload 内容不存在或完整性校验失败"
            ) from exc
        return {
            "artifact_ref": value,
            "storage_ref": storage_ref,
            "content_sha256": digest,
            "basename": filename,
            "kind": kind,
        }

    def as_import_file(self, artifact_ref: str) -> AssetImportFile:
        item = self.resolve(artifact_ref)
        return AssetImportFile(
            artifact_ref=item["artifact_ref"],
            storage_ref=item["storage_ref"],
            content_sha256=item["content_sha256"],
            filename=item["basename"],
            asset_kind=item["kind"],
        )


@dataclass(frozen=True)
class ResolvedAssetCenter:
    package_id: str
    category_id: str
    package_version: int
    center: AssetCenterService


class AssetPackageResolver:
    def __init__(self, centers: Iterable[AssetCenterService]):
        self._centers = tuple(centers)

    @classmethod
    def from_manifests(
        cls,
        manifests: Iterable[str | Path],
        *,
        storage: LocalAssetStorage,
        package_registry_path: str | Path | None = None,
        packages_root: str | Path | None = None,
        import_catalog_path: str | Path | None = None,
    ) -> "AssetPackageResolver":
        unique = []
        seen = set()
        for raw in manifests:
            path = Path(raw).expanduser().resolve()
            if path in seen:
                continue
            seen.add(path)
            unique.append(AssetCenterService(
                path, storage=storage, package_registry_path=package_registry_path,
                packages_root=packages_root, import_catalog_path=import_catalog_path,
            ))
        return cls(unique)

    def resolve(self, package_id: str) -> ResolvedAssetCenter:
        requested = str(package_id or "").strip()
        matches = [item for item in self._resolved() if item.package_id == requested]
        if not matches:
            raise WorkspaceServiceError("NOT_FOUND", "资产包不存在")
        if len(matches) != 1:
            raise WorkspaceServiceError("PACKAGE_VERSION_CONFLICT", "资产包标识不唯一")
        return matches[0]

    def resolve_active(self, category_id: str) -> ResolvedAssetCenter:
        requested = str(category_id or "").strip()
        matches = [item for item in self._resolved() if item.category_id == requested]
        if not matches:
            raise WorkspaceServiceError("NOT_FOUND", "当前类别没有可用资产包")
        if len(matches) != 1:
            raise WorkspaceServiceError(
                "PACKAGE_VERSION_CONFLICT", "当前类别存在多个活动资产包"
            )
        return matches[0]

    def _resolved(self) -> list[ResolvedAssetCenter]:
        resolved = []
        try:
            for center in self._centers:
                package = center._current_package()
                resolved.append(
                    ResolvedAssetCenter(
                        package_id=str(package.get("package_id") or ""),
                        category_id=str(package.get("category_id") or ""),
                        package_version=int(package.get("package_version") or 0),
                        center=center,
                    )
                )
        except (AssetPackageError, AssetCenterError, OSError, ValueError) as exc:
            raise WorkspaceServiceError(
                "PACKAGE_VERSION_CONFLICT", "资产包清单不可用"
            ) from exc
        return resolved


@dataclass(frozen=True)
class AssetCenterToolServices:
    store: WorkspaceStore
    projects: ProjectService
    tasks: TaskService
    confirmations: ActionConfirmationService
    idempotency: ToolIdempotencyService
    uploads: AssetUploadStore
    packages: AssetPackageResolver


class AssetCenterToolService:
    """E05 service adapter with explicit non-atomic recovery boundaries."""

    def __init__(
        self,
        services: AssetCenterToolServices,
        *,
        checkpoint: Callable[[str], None] | None = None,
    ):
        self.services = services
        self._checkpoint = checkpoint or (lambda _name: None)

    @classmethod
    def from_runtime_config(
        cls,
        *,
        confirmation_signing_key: bytes,
        config: dict | None = None,
    ) -> "AssetCenterToolService":
        runtime = config or load_runtime_config()
        store = WorkspaceStore.from_runtime_config(runtime)
        uploads = AssetUploadStore(runtime["data_root"])
        manifests = list(dict(runtime.get("asset_packages") or {}).values())
        if runtime.get("asset_package"):
            manifests.append(runtime["asset_package"])
        packages = AssetPackageResolver.from_manifests(
            manifests, storage=uploads.storage,
            package_registry_path=runtime.get("package_registry_path"),
            packages_root=runtime.get("packages_root"),
            import_catalog_path=runtime.get("import_catalog_path"),
        )
        return cls(
            AssetCenterToolServices(
                store=store,
                projects=ProjectService(store),
                tasks=TaskService(store),
                confirmations=ActionConfirmationService(
                    store, confirmation_signing_key
                ),
                idempotency=ToolIdempotencyService(store),
                uploads=uploads,
                packages=packages,
            )
        )

    def freeze_upload(self, source: str | Path) -> dict:
        return self.services.uploads.freeze(source)

    def issue_confirmation(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        ttl_seconds: int = 300,
    ) -> dict:
        if tool_name not in _IMPORT_TOOLS:
            raise WorkspaceServiceError("INVALID_INPUT", "不是资产导入写动作")
        normalized = _validated_parameters(tool_name, parameters, for_confirmation=True)
        self._resolve_write_context(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=normalized,
        )
        confirmations = self._confirmations_for(tool_name)
        return confirmations.issue(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=parameters,
            ttl_seconds=ttl_seconds,
        )

    def search_assets(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
    ) -> dict:
        normalized = _validated_parameters("search_assets", parameters)
        session = self._session(
            auth_token,
            agent_session_id=agent_session_id,
            permission="asset.read",
        )
        if normalized["category_id"] != session["category_id"]:
            raise WorkspaceServiceError(
                "PERMISSION_DENIED", "查询类别与当前项目类别不一致"
            )
        package = self.services.packages.resolve_active(normalized["category_id"])
        filters = dict(normalized.get("filters") or {})
        unknown = sorted(set(filters) - {"kind", "limit"})
        if unknown:
            raise WorkspaceServiceError("INVALID_INPUT", "资产查询过滤条件不受支持")
        kind = str(filters.get("kind") or "all")
        limit = filters.get("limit", 20)
        if type(limit) is not int:
            raise WorkspaceServiceError("INVALID_INPUT", "limit 必须是整数")
        try:
            rows = package.center.search_assets(
                normalized["query"],
                kind=kind,
                current_project_id=session["project_id"],
                limit=limit,
            )
        except AssetCenterError as exc:
            raise _workspace_asset_error(exc) from exc
        results = [
            _safe_search_result(item, current_project_id=session["project_id"])
            for item in rows
        ]
        return {
            "project_id": session["project_id"],
            "category_id": package.category_id,
            "package_id": package.package_id,
            "package_version": package.package_version,
            "results": results,
        }

    def create_asset_import(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
        confirmation_token: str,
    ) -> dict:
        return self._controlled_write(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name="create_asset_import",
            parameters=parameters,
            confirmation_token=confirmation_token,
        )

    def review_asset_import(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
        confirmation_token: str,
    ) -> dict:
        return self._controlled_write(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name="review_asset_import",
            parameters=parameters,
            confirmation_token=confirmation_token,
        )

    def publish_asset_import(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
    ) -> dict:
        token = str((parameters or {}).get("confirmation_token") or "")
        return self._controlled_write(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name="publish_asset_import",
            parameters=parameters,
            confirmation_token=token,
        )

    def _controlled_write(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        confirmation_token: str,
    ) -> dict:
        normalized = _validated_parameters(tool_name, parameters)
        context = self._resolve_write_context(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=normalized,
        )
        idempotency = self._idempotency_for(tool_name)
        claim = idempotency.claim(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=parameters,
        )
        if claim["replayed"]:
            if claim["response"] is None:
                raise AssetToolRecoveryRequired(
                    tool_name=tool_name,
                    idempotency_record_id=claim["idempotency_record_id"],
                )
            return self._replayed_result(auth_token, tool_name, claim["response"])

        self._checkpoint("after_idempotency_claim")
        task = self.services.tasks.create_task(
            auth_token,
            agent_session_id=agent_session_id,
            task_type=tool_name,
            payload_sha256=claim["request_sha256"],
            expected_project_version=context["project_version"],
        )
        task = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="executing",
            details={"package_id": context["package"].package_id, "tool_name": tool_name},
        )
        self._checkpoint("after_task_started")
        try:
            self._confirmations_for(tool_name).consume(
                auth_token,
                confirmation_token=confirmation_token,
                agent_session_id=agent_session_id,
                tool_name=tool_name,
                parameters=parameters,
            )
            self._checkpoint("after_confirmation_consumed")
            business_result = self._domain_action(
                tool_name=tool_name,
                parameters=normalized,
                context=context,
                idempotency_record_id=claim["idempotency_record_id"],
            )
            self._checkpoint("after_domain_write")
        except (WorkspaceServiceError, AssetCenterError) as exc:
            error = _public_tool_error(exc)
            self.services.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=task["task_version"],
                status="failed",
                details={"error": error, "tool_name": tool_name},
                error_code=error["code"],
            )
            stored = idempotency.record_result(
                auth_token,
                idempotency_record_id=claim["idempotency_record_id"],
                result={
                    "task_id": task["task_id"],
                    "status": "failed",
                    "artifact_refs": [],
                    "warnings": [],
                    "error": error,
                },
            )
            return {
                "tool_name": tool_name,
                "task_id": task["task_id"],
                "status": "failed",
                "replayed": stored["replayed"],
                "result": None,
                "error": error,
            }

        logical_ref = (
            f"artifact://asset-import/{business_result['import_id']}/"
            f"r{business_result['revision']}"
        )
        warnings = []
        if business_result["state"] == "staged":
            warnings.append("机器预检通过不等于人工审核通过，导入仍不可发布")
        finished = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="completed",
            details={"result": business_result, "tool_name": tool_name},
            result_ref=logical_ref,
        )
        self._checkpoint("after_task_completed")
        stored = idempotency.record_result(
            auth_token,
            idempotency_record_id=claim["idempotency_record_id"],
            result={
                "task_id": finished["task_id"],
                "status": "completed",
                "artifact_refs": [logical_ref],
                "warnings": warnings,
                "error": None,
            },
        )
        return {
            "tool_name": tool_name,
            "task_id": finished["task_id"],
            "status": "completed",
            "replayed": stored["replayed"],
            "result": business_result,
            "error": None,
        }

    def _confirmations_for(self, tool_name: str):
        if tool_name != "publish_asset_import":
            return self.services.confirmations
        return ManualPageConfirmationService(
            self.services.store, self.services.confirmations.signing_key
        )

    def _idempotency_for(self, tool_name: str):
        if tool_name != "publish_asset_import":
            return self.services.idempotency
        return ManualPageIdempotencyService(self.services.store)

    def _resolve_write_context(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
    ) -> dict:
        permission = {
            "create_asset_import": "asset.import",
            "review_asset_import": "asset.review",
            "publish_asset_import": "asset.publish",
        }[tool_name]
        session = self._session(
            auth_token,
            agent_session_id=agent_session_id,
            permission=permission,
        )
        package = self.services.packages.resolve(parameters["package_id"])
        if package.category_id != session["category_id"]:
            raise WorkspaceServiceError(
                "PERMISSION_DENIED", "资产包类别与当前项目类别不一致"
            )
        if package.package_version != parameters["expected_active_package_version"]:
            raise WorkspaceServiceError(
                "PACKAGE_VERSION_CONFLICT", "活动资产包版本已变化"
            )
        if package.center.storage.root != self.services.uploads.storage.root:
            raise WorkspaceServiceError(
                "INVALID_INPUT", "资产中心与 upload 内容存储未绑定到同一根目录"
            )
        if tool_name == "create_asset_import":
            if parameters["category_id"] != session["category_id"]:
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED", "导入类别与当前项目类别不一致"
                )
            for artifact_ref in parameters["file_refs"]:
                self.services.uploads.resolve(artifact_ref)
        else:
            try:
                record = package.center.get_import(parameters["import_id"])
            except AssetCenterError as exc:
                raise _workspace_asset_error(exc) from exc
            if record["package_id"] != package.package_id:
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED", "导入记录不属于工具指定资产包"
                )
            if record["category_id"] != session["category_id"]:
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED", "导入记录类别与当前项目类别不一致"
                )
            if (
                record["scope"] == "project"
                and record["target_project_id"] != session["project_id"]
            ):
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED", "项目级导入不属于当前项目"
                )
        return {**session, "package": package}

    def _session(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        permission: str,
    ) -> dict:
        session = self.services.tasks.get_agent_session(
            auth_token, agent_session_id
        )
        project = self.services.projects.get_project(
            auth_token, session["project_id"]
        )
        with self.services.store.connection() as connection:
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                permission=permission,
            )
            if actor["actor_id"] != session["actor_id"]:
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED", "不能使用其他操作者的 agent session"
                )
        return {
            **session,
            "workspace_id": project["workspace_id"],
            "category_id": project["category_id"],
            "project_version": project["project_version"],
        }

    def _domain_action(
        self,
        *,
        tool_name: str,
        parameters: dict[str, Any],
        context: dict,
        idempotency_record_id: str,
    ) -> dict:
        center = context["package"].center
        if tool_name == "create_asset_import":
            import_id = "import_" + idempotency_record_id.split("_", 1)[-1]
            files = [
                self.services.uploads.as_import_file(ref)
                for ref in parameters["file_refs"]
            ]
            return center.stage_import(
                import_id=import_id,
                category_id=parameters["category_id"],
                scope=parameters["scope"],
                project_id=(
                    context["project_id"] if parameters["scope"] == "project" else None
                ),
                files=files,
                requested_by_actor_id=context["actor_id"],
                expected_package_version=parameters["expected_active_package_version"],
            )
        if tool_name == "review_asset_import":
            return center.review_import(
                parameters["import_id"],
                reviewed_by_actor_id=context["actor_id"],
                expected_revision=parameters["expected_import_revision"],
            )
        return center.publish_import(
            parameters["import_id"],
            published_by_actor_id=context["actor_id"],
            expected_revision=parameters["expected_import_revision"],
        )

    def _replayed_result(self, auth_token: str, tool_name: str, stored: dict) -> dict:
        if stored["status"] == "completed":
            events = self.services.tasks.list_task_events(
                auth_token, stored["task_id"]
            )
            completed = [event for event in events if event["status"] == "completed"]
            if not completed or "result" not in completed[-1]["details"]:
                raise AssetToolRecoveryRequired(
                    tool_name=tool_name,
                    idempotency_record_id="recorded-result-missing-task-evidence",
                )
            result = completed[-1]["details"]["result"]
        else:
            result = None
        return {
            "tool_name": tool_name,
            "task_id": stored["task_id"],
            "status": stored["status"],
            "replayed": True,
            "result": result,
            "error": stored.get("error"),
        }


def _validated_parameters(
    tool_name: str,
    parameters: dict[str, Any],
    *,
    for_confirmation: bool = False,
) -> dict:
    payload = dict(parameters or {})
    if for_confirmation and tool_name == "publish_asset_import":
        payload.setdefault("confirmation_token", "confirmation-placeholder-00000000")
    try:
        validator = validate_manual_action_input if tool_name == "publish_asset_import" else validate_tool_input
        normalized = validator(tool_name, payload).model_dump(mode="json")
    except (ValidationError, TypeError, ValueError) as exc:
        raise WorkspaceServiceError("INVALID_INPUT", "动作参数不符合工具合同") from exc
    if for_confirmation:
        normalized.pop("confirmation_token", None)
    return normalized


def _asset_kind(filename: str) -> str:
    suffix = Path(str(filename or "")).suffix.casefold()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix == ".xlsx":
        return "workbook"
    raise WorkspaceServiceError("INVALID_INPUT", "仅支持 .xlsx 和受支持的图片格式")


def _safe_search_result(item: dict, *, current_project_id: str) -> dict:
    source = dict(item.get("source") or {})
    import_id = str(source.get("import_id") or "")
    if import_id:
        scope = str(source.get("scope") or "")
        target = str(source.get("target_project_id") or "")
        if item.get("workflow_state") != "published" or scope not in {"shared", "project"}:
            raise WorkspaceServiceError("PERMISSION_DENIED", "搜索结果包含未发布导入")
        if scope == "project" and target != current_project_id:
            raise WorkspaceServiceError("PERMISSION_DENIED", "搜索结果包含其他项目资产")
    safe_source = {
        key: source[key]
        for key in (
            "artifact_ref",
            "package_id",
            "package_version",
            "import_id",
            "scope",
            "target_project_id",
            "source_file",
            "source_sheet",
            "source_row",
            "source_links",
        )
        if key in source
    }
    result = {
        key: item[key]
        for key in (
            "asset_id",
            "asset_kind",
            "title",
            "content_sha256",
            "storage_ref",
            "availability",
            "workflow_state",
        )
        if key in item
    }
    result["source"] = safe_source
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if re.search(r"(?:^|[\"'])/(?:Users|Applications|private|tmp)/", serialized):
        raise WorkspaceServiceError("PERMISSION_DENIED", "搜索结果包含本机绝对路径")
    return result


def _workspace_asset_error(exc: AssetCenterError) -> WorkspaceServiceError:
    code = str(exc.code)
    if code in {"MACHINE_REVIEW_FAILED", "INVALID_STATE"}:
        code = "REVIEW_REQUIRED"
    elif code == "IMMUTABLE_PACKAGE_VERSION":
        code = "PACKAGE_VERSION_CONFLICT"
    elif code.startswith("STORAGE_"):
        code = "INVALID_INPUT"
    return WorkspaceServiceError(code, str(exc))


def _public_tool_error(exc: WorkspaceServiceError | AssetCenterError) -> dict:
    if isinstance(exc, AssetCenterError):
        converted = _workspace_asset_error(exc)
    else:
        converted = exc
    stable = {
        "INVALID_INPUT",
        "NOT_FOUND",
        "REVIEW_REQUIRED",
        "PERMISSION_DENIED",
        "WORKSPACE_VERSION_CONFLICT",
        "PROJECT_VERSION_CONFLICT",
        "PACKAGE_VERSION_CONFLICT",
        "IDEMPOTENCY_CONFLICT",
    }
    code = converted.code if converted.code in stable else "INTERNAL_ERROR"
    return {
        "code": code,
        "message": converted.message,
        "retryable": code in {"PROJECT_VERSION_CONFLICT", "PACKAGE_VERSION_CONFLICT"},
        "details": {},
    }
