from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from .action_confirmation import ActionConfirmationService
from .agent_tools import validate_tool_input
from .asset_package import AssetPackageError, package_script_catalog
from .brief_artifact_store import BriefArtifactStore
from .creative_strategy import (
    CreativeStrategyError,
    STRATEGY_VERSION,
    build_creative_strategies,
    select_creative_strategy,
)
from .creative_strategy_artifacts import CreativeStrategyArtifactStore
from .identity_service import require_permission
from .project_service import ProjectService
from .run_contract import sha256_json
from .runtime_config import configured_asset_package, load_runtime_config
from .script_type_adapter import metadata_for_category_id
from .task_service import TaskService
from .tool_idempotency import ToolIdempotencyService
from .workspace_store import WorkspaceServiceError, WorkspaceStore


class CreativeStrategyRecoveryRequired(RuntimeError):
    code = "RECOVERY_RECONCILIATION_REQUIRED"

    def __init__(self, *, idempotency_record_id: str):
        self.idempotency_record_id = idempotency_record_id
        self.retryable = False
        self.recovery = {
            "automatic_retry": False,
            "reason": "Brief 快照写入与幂等结果分属不同事务，无法安全判断选择是否已经生效",
            "required_checks": [
                "核对 project_brief_snapshots、projects.current_brief_snapshot_id 与 task_events",
                "由管理员人工补记幂等结果，或确认未写入后使用新幂等键",
            ],
        }
        super().__init__(f"{self.code}: 策略选择结果不确定，已阻断自动重放")


class CreativeStrategyPackageResolver:
    """Resolve only the active configured ai-spec package for a project category."""

    def __init__(self, config: dict):
        self.config = dict(config)

    def load(self, category_id: str) -> dict:
        try:
            metadata = metadata_for_category_id(category_id)
        except ValueError as exc:
            raise WorkspaceServiceError("INVALID_INPUT", str(exc)) from exc
        manifest = configured_asset_package(metadata.display_name, self.config)
        if not manifest:
            raise WorkspaceServiceError(
                "NOT_FOUND", f"{metadata.display_name}尚未配置活动资产包"
            )
        try:
            template, _creative, source = package_script_catalog(
                manifest,
                expected_category_id=category_id,
                source_view="ai-spec",
            )
        except (AssetPackageError, OSError, ValueError) as exc:
            raise WorkspaceServiceError(
                "PACKAGE_VERSION_CONFLICT", f"活动资产包或 AI_规范表不可用: {exc}"
            ) from exc
        return {
            "template_slots": template,
            "binding": {
                "package_id": source["package_id"],
                "category_id": category_id,
                "source_view": source["source_view"],
                "source_workbook_sha256": source["source_workbook_sha256"],
                "index_fingerprint": source["index_fingerprint"],
            },
        }


@dataclass(frozen=True)
class CreativeStrategyToolServices:
    store: WorkspaceStore
    projects: ProjectService
    tasks: TaskService
    confirmations: ActionConfirmationService
    idempotency: ToolIdempotencyService
    briefs: BriefArtifactStore
    portfolios: CreativeStrategyArtifactStore
    packages: CreativeStrategyPackageResolver


class CreativeStrategyToolService:
    """E06 adapter over authoritative project, Brief and ai-spec package state."""

    def __init__(
        self,
        services: CreativeStrategyToolServices,
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
    ) -> "CreativeStrategyToolService":
        runtime = config or load_runtime_config()
        store = WorkspaceStore.from_runtime_config(runtime)
        return cls(
            CreativeStrategyToolServices(
                store=store,
                projects=ProjectService(store),
                tasks=TaskService(store),
                confirmations=ActionConfirmationService(
                    store, confirmation_signing_key
                ),
                idempotency=ToolIdempotencyService(store),
                briefs=BriefArtifactStore(runtime["data_root"]),
                portfolios=CreativeStrategyArtifactStore(runtime["data_root"]),
                packages=CreativeStrategyPackageResolver(runtime),
            )
        )

    def read_creative_strategies(self, auth_token: str, *, agent_session_id: str) -> dict:
        """Read registered frozen results; this method creates no task or artifact."""
        current = self._current_project_input(
            auth_token, agent_session_id=agent_session_id, permission="project.diagnose",
        )
        return self._read_strategy_input(current)

    def _read_strategy_input(self, current: dict) -> dict:
        with self.services.store.connection() as connection:
            rows = connection.execute(
                "SELECT task_id, result_ref FROM tasks WHERE project_id=? AND actor_id=? "
                "AND task_type='get_creative_strategies' AND status='completed' "
                "ORDER BY created_at DESC, task_id DESC", (current["project_id"], current["actor_id"]),
            ).fetchall()
        stale = None
        for row in rows:
            try:
                artifact = self.services.portfolios.read(row["result_ref"])
            except (WorkspaceServiceError, OSError, ValueError):
                stale = stale or {"task_id": row["task_id"], "reason": "artifact_unavailable"}
                continue
            binding = artifact["bindings"]
            matches = (
                binding.get("project_id") == current["project_id"]
                and binding.get("project_version") == current["project_version"]
                and binding.get("brief_ref") == current["brief_snapshot"]["brief_ref"]
                and binding.get("plan_sha256") == artifact["portfolio"].get("plan_sha256")
                and binding.get("brief_snapshot_id") == current["brief_snapshot"]["brief_snapshot_id"]
                and binding.get("brief_sha256") == current["brief_snapshot"]["brief_sha256"]
                and binding.get("package") == current["package_binding"]
                and binding.get("strategy_version") == STRATEGY_VERSION
                and artifact["portfolio"].get("strategy_version") == STRATEGY_VERSION
            )
            if matches:
                return {"status": "available", "project_id": current["project_id"],
                        "project_version": current["project_version"], "task_id": row["task_id"],
                        "portfolio": artifact["portfolio"], "portfolio_ref": artifact["portfolio_ref"],
                        "plan_sha256": binding["plan_sha256"], "warnings": [], "error": None,
                        "provider_calls": 0}
            stale = stale or {"task_id": row["task_id"], "reason": "input_changed"}
        return {"status": "stale" if stale else "not_analyzed",
                "project_id": current["project_id"], "project_version": current["project_version"],
                "portfolio": None, "previous_result": stale, "warnings": [], "error": None,
                "provider_calls": 0}

    def _strategy_task_result(self, auth_token: str, task: dict) -> dict:
        result = None
        if task["status"] == "completed":
            events = self.services.tasks.list_task_events(auth_token, task["task_id"])
            result = next((event["details"]["result"] for event in reversed(events)
                           if event["status"] == "completed" and "result" in event["details"]), None)
        return {"tool_name": "get_creative_strategies", "task_id": task["task_id"],
                "status": task["status"], "artifact_refs": [task["result_ref"]] if task.get("result_ref") else [],
                "warnings": [], "replayed": True, "result": result,
                "error": {"code": task["error_code"], "message": "原分析任务未完成，请查看原任务"}
                         if task.get("error_code") else None}

    def get_creative_strategies(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
    ) -> dict:
        normalized = _validated_parameters("get_creative_strategies", parameters)
        context = self._session(
            auth_token,
            agent_session_id=agent_session_id,
            permission="project.diagnose",
        )
        if normalized["project_id"] != context["project_id"]:
            raise WorkspaceServiceError(
                "PERMISSION_DENIED", "动作 project_id 与 agent session 项目不一致"
            )
        current = self._current_project_input(
            auth_token, agent_session_id=agent_session_id, permission="project.diagnose",
        )
        existing = self._read_strategy_input(current)
        if existing["status"] == "available":
            return self._strategy_task_result(
                auth_token, self.services.tasks.get_task(auth_token, existing["task_id"]),
            )
        input_binding = {
            "parameters": normalized, "project_version": current["project_version"],
            "brief_snapshot_id": current["brief_snapshot"]["brief_snapshot_id"],
            "brief_sha256": current["brief_snapshot"]["brief_sha256"],
            "package": current["package_binding"], "strategy_version": STRATEGY_VERSION,
        }
        task = self.services.tasks.create_task(
            auth_token,
            agent_session_id=agent_session_id,
            task_type="get_creative_strategies",
            payload_sha256=sha256_json(input_binding),
            expected_project_version=current["project_version"],
            reuse_payload=True,
        )
        if task.get("replayed"):
            if task["status"] == "completed":
                # The result was not valid in the read above: never return a
                # corrupt/missing artifact as a successful analysis replay.
                checked = self._read_strategy_input(current)
                if checked["status"] != "available":
                    return {"tool_name": "get_creative_strategies", "task_id": task["task_id"],
                            "status": "blocked", "artifact_refs": [], "warnings": [],
                            "replayed": True, "result": None,
                            "error": {"code": "ARTIFACT_HASH_MISMATCH",
                                      "message": "原策略结果不可验证，请核对原任务产物"}}
            return self._strategy_task_result(auth_token, task)
        executing = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="executing",
            details={"provider_calls": 0, "source_view": "ai-spec"},
        )
        try:
            portfolio = build_creative_strategies(
                current["brief"],
                current["template_slots"],
                source_view="ai-spec",
            )
            binding = {
                "project_id": current["project_id"],
                "project_version": current["project_version"],
                "brief_snapshot_id": current["brief_snapshot"]["brief_snapshot_id"],
                "brief_ref": current["brief_snapshot"]["brief_ref"],
                "brief_sha256": current["brief_snapshot"]["brief_sha256"],
                "package": current["package_binding"],
                "plan_sha256": portfolio["plan_sha256"],
                "strategy_version": portfolio["strategy_version"],
            }
            artifact = self.services.portfolios.freeze(portfolio, bindings=binding)
            result = {
                "project_id": current["project_id"],
                "project_version": current["project_version"],
                **artifact,
                "plan_sha256": portfolio["plan_sha256"],
                "portfolio": portfolio,
                "package": current["package_binding"],
                "provider_calls": 0,
            }
        except (
            CreativeStrategyError,
            WorkspaceServiceError,
            OSError,
            ValueError,
        ) as exc:
            if isinstance(exc, OSError):
                exc = WorkspaceServiceError(
                    "ARTIFACT_WRITE_FAILED", "策略 portfolio 无法写入"
                )
            elif not isinstance(exc, (CreativeStrategyError, WorkspaceServiceError)):
                exc = WorkspaceServiceError("INVALID_INPUT", str(exc))
            return self._fail_task(
                auth_token,
                task=executing,
                tool_name="get_creative_strategies",
                error=_strategy_error(exc),
            )
        warnings = []
        if portfolio["status"] != "pass":
            warnings.append("策略比较尚未就绪；需先补齐 Brief 事实或策略证据")
        finished = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=executing["task_version"],
            status="completed",
            details={"result": result, "provider_calls": 0},
            result_ref=artifact["portfolio_ref"],
        )
        return {
            "tool_name": "get_creative_strategies",
            "task_id": finished["task_id"],
            "status": "completed",
            "artifact_refs": [artifact["portfolio_ref"]],
            "warnings": warnings,
            "replayed": False,
            "result": result,
            "error": None,
        }

    def issue_selection_confirmation(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
        ttl_seconds: int = 300,
    ) -> dict:
        normalized = _validated_parameters("select_creative_strategy", parameters)
        self._selection_input(
            auth_token,
            agent_session_id=agent_session_id,
            parameters=normalized,
        )
        return self.services.confirmations.issue(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name="select_creative_strategy",
            parameters=parameters,
            ttl_seconds=ttl_seconds,
        )

    def select_creative_strategy(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
        confirmation_token: str,
    ) -> dict:
        tool_name = "select_creative_strategy"
        normalized = _validated_parameters(tool_name, parameters)
        session = self._session(
            auth_token,
            agent_session_id=agent_session_id,
            permission="run.generate",
        )
        if normalized["project_id"] != session["project_id"]:
            raise WorkspaceServiceError(
                "PERMISSION_DENIED", "动作 project_id 与 agent session 项目不一致"
            )
        if self._existing_idempotency_record(session, normalized):
            claim = self.services.idempotency.claim(
                auth_token,
                agent_session_id=agent_session_id,
                tool_name=tool_name,
                parameters=parameters,
            )
            if claim["response"] is None:
                raise CreativeStrategyRecoveryRequired(
                    idempotency_record_id=claim["idempotency_record_id"]
                )
            return self._replayed_result(auth_token, claim["response"])

        self._selection_input(
            auth_token,
            agent_session_id=agent_session_id,
            parameters=normalized,
        )
        claim = self.services.idempotency.claim(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=parameters,
        )
        if claim["replayed"]:
            if claim["response"] is None:
                raise CreativeStrategyRecoveryRequired(
                    idempotency_record_id=claim["idempotency_record_id"]
                )
            return self._replayed_result(auth_token, claim["response"])

        self._checkpoint("after_idempotency_claim")
        context = self._selection_input(
            auth_token,
            agent_session_id=agent_session_id,
            parameters=normalized,
        )
        task = self.services.tasks.create_task(
            auth_token,
            agent_session_id=agent_session_id,
            task_type=tool_name,
            payload_sha256=claim["request_sha256"],
            expected_project_version=context["project_version"],
        )
        executing = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="executing",
            details={"provider_calls": 0, "strategy_id": normalized["strategy_id"]},
        )
        self._checkpoint("after_task_started")
        try:
            self.services.confirmations.consume(
                auth_token,
                confirmation_token=confirmation_token,
                agent_session_id=agent_session_id,
                tool_name=tool_name,
                parameters=parameters,
            )
            self._checkpoint("after_confirmation_consumed")
            context = self._selection_input(
                auth_token,
                agent_session_id=agent_session_id,
                parameters=normalized,
            )
            selection = select_creative_strategy(
                context["brief"],
                context["template_slots"],
                portfolio=context["portfolio_artifact"]["portfolio"],
                plan_sha256=normalized["plan_sha256"],
                strategy_id=normalized["strategy_id"],
                expected_strategy_version=context["portfolio_artifact"]["bindings"][
                    "strategy_version"
                ],
                source_view="ai-spec",
            )
            derived_brief = self._derived_brief(
                context=context,
                selection=selection,
                portfolio_ref=normalized["strategy_portfolio_ref"],
            )
            frozen = self.services.briefs.freeze(derived_brief)
            snapshot = self.services.projects.add_brief_snapshot(
                auth_token,
                project_id=context["project_id"],
                brief_ref=frozen["brief_ref"],
                brief_sha256=frozen["brief_sha256"],
                expected_project_version=context["project_version"],
            )
            result = {
                "project_id": context["project_id"],
                "project_version": snapshot["project_version"],
                "portfolio_ref": normalized["strategy_portfolio_ref"],
                "brief_snapshot": snapshot,
                "selection": selection,
                "provider_calls": 0,
            }
            self._checkpoint("after_domain_write")
        except (
            CreativeStrategyError,
            WorkspaceServiceError,
            OSError,
            ValueError,
        ) as exc:
            if isinstance(exc, OSError):
                exc = WorkspaceServiceError(
                    "ARTIFACT_WRITE_FAILED", "派生 Brief artifact 无法写入"
                )
            elif not isinstance(exc, (CreativeStrategyError, WorkspaceServiceError)):
                exc = WorkspaceServiceError("INVALID_INPUT", str(exc))
            error = _strategy_error(exc)
            failed = self.services.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=executing["task_version"],
                status="failed",
                details={"error": error, "provider_calls": 0},
                error_code=error["code"],
            )
            stored = self.services.idempotency.record_result(
                auth_token,
                idempotency_record_id=claim["idempotency_record_id"],
                result={
                    "task_id": failed["task_id"],
                    "status": "failed",
                    "artifact_refs": [],
                    "warnings": [],
                    "error": error,
                },
            )
            return {
                "tool_name": tool_name,
                "task_id": failed["task_id"],
                "status": "failed",
                "artifact_refs": [],
                "warnings": [],
                "replayed": stored["replayed"],
                "result": None,
                "error": error,
            }

        artifact_refs = [
            normalized["strategy_portfolio_ref"],
            frozen["brief_ref"],
        ]
        finished = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=executing["task_version"],
            status="completed",
            details={"result": result, "provider_calls": 0},
            result_ref=frozen["brief_ref"],
        )
        self._checkpoint("after_task_completed")
        stored = self.services.idempotency.record_result(
            auth_token,
            idempotency_record_id=claim["idempotency_record_id"],
            result={
                "task_id": finished["task_id"],
                "status": "completed",
                "artifact_refs": artifact_refs,
                "warnings": ["已创建派生 Brief 快照；尚未生成脚本或发布版本"],
                "error": None,
            },
        )
        return {
            "tool_name": tool_name,
            "task_id": finished["task_id"],
            "status": "completed",
            "artifact_refs": artifact_refs,
            "warnings": ["已创建派生 Brief 快照；尚未生成脚本或发布版本"],
            "replayed": stored["replayed"],
            "result": result,
            "error": None,
        }

    def _selection_input(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
    ) -> dict:
        current = self._current_project_input(
            auth_token,
            agent_session_id=agent_session_id,
            permission="run.generate",
        )
        if parameters["project_id"] != current["project_id"]:
            raise WorkspaceServiceError(
                "PERMISSION_DENIED", "动作 project_id 与 agent session 项目不一致"
            )
        if parameters["expected_project_version"] != current["project_version"]:
            raise WorkspaceServiceError(
                "PROJECT_VERSION_CONFLICT", "策略 portfolio 对应的项目版本已变化"
            )
        artifact = self.services.portfolios.read(
            parameters["strategy_portfolio_ref"]
        )
        expected_binding = {
            "project_id": current["project_id"],
            "project_version": current["project_version"],
            "brief_snapshot_id": current["brief_snapshot"]["brief_snapshot_id"],
            "brief_ref": current["brief_snapshot"]["brief_ref"],
            "brief_sha256": current["brief_snapshot"]["brief_sha256"],
            "package": current["package_binding"],
        }
        bindings = artifact["bindings"]
        for key, value in expected_binding.items():
            if bindings.get(key) != value:
                raise WorkspaceServiceError(
                    "PROJECT_VERSION_CONFLICT",
                    "策略 portfolio 与当前项目、Brief 或活动资产包不一致",
                )
        if (
            parameters["plan_sha256"] != bindings.get("plan_sha256")
            or parameters["plan_sha256"]
            != artifact["portfolio"].get("plan_sha256")
        ):
            raise WorkspaceServiceError(
                "INVALID_INPUT", "plan_sha256 与策略 portfolio 不一致"
            )
        portfolio = artifact["portfolio"]
        direction = next(
            (
                item
                for item in portfolio.get("directions") or []
                if item.get("strategy_id") == parameters["strategy_id"]
            ),
            None,
        )
        if (
            not portfolio.get("coverage", {}).get("comparison_ready")
            or direction is None
            or not direction.get("selectable")
        ):
            raise WorkspaceServiceError(
                "REVIEW_REQUIRED", "策略比较尚未就绪或所选方向不可选择"
            )
        return {**current, "portfolio_artifact": artifact}

    def _existing_idempotency_record(
        self, session: dict, parameters: dict[str, Any]
    ) -> bool:
        with self.services.store.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM tool_idempotency_records
                WHERE actor_id = ? AND scope_type = 'project' AND scope_id = ?
                  AND tool_name = 'select_creative_strategy'
                  AND idempotency_key = ?
                """,
                (
                    session["actor_id"],
                    session["project_id"],
                    parameters["idempotency_key"],
                ),
            ).fetchone()
        return row is not None

    def _current_project_input(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        permission: str,
    ) -> dict:
        context = self._session(
            auth_token,
            agent_session_id=agent_session_id,
            permission=permission,
        )
        snapshot = self.services.projects.get_current_brief_snapshot(
            auth_token, context["project_id"]
        )
        if snapshot is None:
            raise WorkspaceServiceError(
                "BRIEF_ARTIFACT_UNAVAILABLE", "项目没有当前 Brief snapshot"
            )
        try:
            brief = self.services.briefs.read(
                snapshot["brief_ref"], snapshot["brief_sha256"]
            )
        except OSError as exc:
            raise WorkspaceServiceError(
                "BRIEF_ARTIFACT_UNAVAILABLE", "当前 Brief artifact 无法读取"
            ) from exc
        package = self.services.packages.load(context["category_id"])
        return {
            **context,
            "brief_snapshot": snapshot,
            "brief": brief,
            "template_slots": package["template_slots"],
            "package_binding": package["binding"],
        }

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

    @staticmethod
    def _derived_brief(
        *,
        context: dict,
        selection: dict,
        portfolio_ref: str,
    ) -> dict:
        derived = deepcopy(context["brief"])
        derived["planning_events"] = deepcopy(selection["planning_events"])
        derived["creative_strategy_selection"] = {
            "schema_version": "mplus-brief-strategy-binding/v1",
            "portfolio_ref": portfolio_ref,
            "portfolio_artifact_sha256": context["portfolio_artifact"][
                "artifact_sha256"
            ],
            "source_brief_snapshot_id": context["brief_snapshot"][
                "brief_snapshot_id"
            ],
            "source_brief_ref": context["brief_snapshot"]["brief_ref"],
            "source_brief_sha256": context["brief_snapshot"]["brief_sha256"],
            "source_project_version": context["project_version"],
            "package": deepcopy(context["package_binding"]),
            "binding": deepcopy(selection["binding"]),
            "constraints": deepcopy(selection["constraints"]),
            "selection_sha256": selection["selection_sha256"],
            "provider_calls": 0,
        }
        return derived

    def _replayed_result(self, auth_token: str, stored: dict) -> dict:
        if stored["status"] == "completed":
            events = self.services.tasks.list_task_events(
                auth_token, stored["task_id"]
            )
            completed = [event for event in events if event["status"] == "completed"]
            if not completed or "result" not in completed[-1]["details"]:
                raise CreativeStrategyRecoveryRequired(
                    idempotency_record_id="recorded-result-missing-task-evidence"
                )
            result = completed[-1]["details"]["result"]
        else:
            result = None
        return {
            "tool_name": "select_creative_strategy",
            "task_id": stored["task_id"],
            "status": stored["status"],
            "artifact_refs": list(stored.get("artifact_refs") or []),
            "warnings": list(stored.get("warnings") or []),
            "replayed": True,
            "result": result,
            "error": stored.get("error"),
        }

    def _fail_task(
        self,
        auth_token: str,
        *,
        task: dict,
        tool_name: str,
        error: dict,
    ) -> dict:
        failed = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="failed",
            details={"error": error, "provider_calls": 0},
            error_code=error["code"],
        )
        return {
            "tool_name": tool_name,
            "task_id": failed["task_id"],
            "status": "failed",
            "artifact_refs": [],
            "warnings": [],
            "replayed": False,
            "result": None,
            "error": error,
        }


def _validated_parameters(tool_name: str, parameters: dict[str, Any]) -> dict:
    try:
        return validate_tool_input(tool_name, parameters).model_dump(mode="json")
    except (ValidationError, TypeError, ValueError) as exc:
        raise WorkspaceServiceError(
            "INVALID_INPUT", "动作参数不符合工具合同"
        ) from exc


def _strategy_error(
    exc: CreativeStrategyError | WorkspaceServiceError,
) -> dict:
    source_code = str(exc.code)
    stable = {
        "INVALID_INPUT",
        "NOT_FOUND",
        "REVIEW_REQUIRED",
        "PERMISSION_DENIED",
        "PROJECT_VERSION_CONFLICT",
        "PACKAGE_VERSION_CONFLICT",
        "IDEMPOTENCY_CONFLICT",
        "BRIEF_ARTIFACT_UNAVAILABLE",
    }
    if isinstance(exc, CreativeStrategyError):
        if source_code in {
            "STRATEGY_COMPARISON_NOT_READY",
            "STRATEGY_NOT_SELECTABLE",
            "RECOMPILED_PLAN_REJECTED",
        }:
            code = "REVIEW_REQUIRED"
        else:
            code = "INVALID_INPUT"
        message = str(exc)
    else:
        code = source_code if source_code in stable else "INTERNAL_ERROR"
        message = exc.message
    return {
        "code": code,
        "message": message,
        "retryable": code in {
            "PROJECT_VERSION_CONFLICT",
            "PACKAGE_VERSION_CONFLICT",
        },
        "details": {"source_code": source_code},
    }
