from __future__ import annotations

"""Isolated, project-centred API for the 48H Alpha candidate.

This module deliberately contains no publication, asset-publication, deletion or
CURRENT operation.  It only projects the persistent E01/E02/E03 records that
already exist in an explicitly isolated runtime.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Callable

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from .alpha_integration import AlphaProjectFlow
from .alpha_image_flow import AlphaImageFlow
from .executable_agent import ExecutableAgentService
from .readonly_agent import ReadonlyAgentService
from .workspace_store import WorkspaceServiceError


_HIGH_RISK_TERMS = re.compile(r"(发布|删除|公司知识|强制恢复|CURRENT)")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
VerifiedBriefReader = Callable[[str, str, str], dict]


class AlphaIsolationError(RuntimeError):
    code = "ALPHA_ISOLATION_REQUIRED"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class AlphaRuntime:
    """Explicitly injected paths for a non-production Alpha process."""

    root: Path
    port: int

    @classmethod
    def isolated(cls, root: str | Path, *, port: int) -> "AlphaRuntime":
        resolved = Path(root).expanduser().resolve()
        production_root = (Path.home() / ".mplus").resolve()
        if resolved == production_root or production_root in resolved.parents:
            raise AlphaIsolationError("Alpha 运行目录不得是或位于真实 ~/.mplus")
        if not 1024 <= port <= 65535:
            raise AlphaIsolationError("Alpha 必须使用独立的非特权端口")
        return cls(root=resolved, port=port)

    @property
    def data_root(self) -> Path:
        return self.root / "data"

    @property
    def runs_root(self) -> Path:
        return self.data_root / "runs"

    def assert_service_isolated(self, service: ReadonlyAgentService) -> None:
        database_path = service.services.store.database_path.expanduser().resolve()
        if not _is_within(database_path, self.data_root):
            raise AlphaIsolationError("Alpha 服务数据库不在隔离 data_root 内")
        resolver_root = service.services.artifacts.runs_root.expanduser().resolve()
        if resolver_root != self.runs_root:
            raise AlphaIsolationError("Alpha 运行事件目录必须是隔离 data_root/runs")


class AlphaConsole:
    """A small facade over the existing persistent project/event services."""

    def __init__(
        self,
        runtime: AlphaRuntime,
        agent: ReadonlyAgentService | ExecutableAgentService,
        *,
        verified_brief_reader: VerifiedBriefReader,
        project_flow: AlphaProjectFlow | None = None,
        image_flow: AlphaImageFlow | None = None,
    ):
        self.runtime = runtime
        self.agent = agent
        self.verified_brief_reader = verified_brief_reader
        self.project_flow = project_flow
        self.image_flow = image_flow
        readonly = agent.services.readonly if isinstance(agent, ExecutableAgentService) else agent
        runtime.assert_service_isolated(readonly)

    def status(self) -> dict:
        return {
            "alpha": "执行中",
            "production_certification": "BLOCK",
            "label": "48H-ALPHA only",
            "runtime": {"storage": "isolated", "events": "persistent", "port": self.runtime.port},
            "forbidden": ["正式发布", "真实 CURRENT", "真实 ~/.mplus", "生产认证"],
        }

    def list_projects(self, auth_token: str, workspace_id: str) -> dict:
        return {"items": self.agent.list_projects(auth_token, workspace_id)}

    def enter_project(
        self,
        auth_token: str,
        *,
        project_id: str,
        brief_ref: str = "",
        expected_sha256: str = "",
    ) -> dict:
        if self.project_flow is not None:
            adapted, snapshot = self.project_flow.bound_planning_input(
                auth_token, project_id=project_id
            )
            brief_ref, expected_sha256 = snapshot["brief_ref"], snapshot["brief_sha256"]
        else:
            adapted = self._verified_brief(
                auth_token, project_id=project_id, brief_ref=brief_ref,
                expected_sha256=expected_sha256,
            )
        state = self.project_dashboard(auth_token, project_id=project_id)
        return {
            "alpha": "执行中",
            "production_certification": "BLOCK",
            "project": state["project"],
            "brief": {"ref": brief_ref, "sha256": expected_sha256, "adapted": True, "field_count": len(adapted)},
        }

    def create_session(
        self,
        auth_token: str,
        *,
        project_id: str,
        brief_ref: str = "",
        expected_sha256: str = "",
    ) -> dict:
        if self.project_flow is not None:
            self.project_flow.bound_planning_input(auth_token, project_id=project_id)
        else:
            self._verified_brief(
                auth_token, project_id=project_id, brief_ref=brief_ref,
                expected_sha256=expected_sha256,
            )
        return self.agent.create_session(auth_token, project_id)

    def project_dashboard(
        self,
        auth_token: str,
        *,
        project_id: str,
        agent_session_id: str | None = None,
    ) -> dict:
        state = self.agent.get_project_state(auth_token, project_id)
        timeline: list[dict] = []
        if agent_session_id:
            session = self.agent.get_session(auth_token, agent_session_id)
            if session["session"]["project_id"] != project_id:
                raise WorkspaceServiceError("PERMISSION_DENIED", "session 不属于当前项目")
            timeline = list(session["timeline"])
        return {
            "alpha": "执行中",
            "production_certification": "BLOCK",
            "project": state["project"],
            "brief": state["brief"],
            "run": state["run"],
            "versions": state["versions"],
            "blocker": state["blocker"],
            "next_steps": state["next_steps"],
            "timeline": timeline,
        }

    def problem_shots(self, auth_token: str, *, project_id: str) -> dict:
        """Project-scoped, SHA-verified audit facts; never synthetic review items."""
        state = self.agent.get_project_state(auth_token, project_id)
        run_id = str((state.get("run") or {}).get("run_id") or "")
        if not run_id:
            return {"run_id": None, "items": [], "status": "no_verified_run"}
        artifact = self._readonly().services.artifacts.resolve_artifact(run_id, "run_report")
        try:
            report = json.loads(artifact.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "已验证 run_report 无法读取") from exc
        items = []
        for raw in list(report.get("blocking_items") or []):
            if isinstance(raw, dict):
                shot_no = str(raw.get("shot_no") or raw.get("shot") or "") or None
                code = str(raw.get("code") or raw.get("id") or "UNKNOWN")
                message = str(raw.get("message") or raw.get("detail") or code)
            else:
                shot_no, code, message = None, str(raw), str(raw)
            items.append({"shot_no": shot_no, "code": code, "message": message})
        return {
            "run_id": run_id,
            "status": "needs_human_action" if items else "no_machine_blocking_items",
            "items": items,
            "evidence": {"ref": artifact.ref, "sha256": artifact.sha256},
            "notice": "机器问题只用于定位；是否通过仍由人工整片确认。",
        }

    def submit_message(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        content: str,
        run_id: str | None = None,
    ) -> dict:
        if _HIGH_RISK_TERMS.search(str(content or "")):
            raise WorkspaceServiceError("ALPHA_HIGH_RISK_FORBIDDEN", "Alpha 中控台不提供高风险 Agent 动作")
        return self.agent.submit_message(
            auth_token, agent_session_id, content=content, run_id=run_id
        )

    def confirm_low_risk_action(
        self,
        auth_token: str,
        *,
        task_id: str,
        proposal_digest: str,
    ) -> dict:
        if not isinstance(self.agent, ExecutableAgentService):
            raise WorkspaceServiceError("ALPHA_EXECUTION_UNAVAILABLE", "Alpha 未注入隔离执行服务")
        return self.agent.confirm_action(
            auth_token, task_id, proposal_digest=proposal_digest
        )

    def confirm_problem_shot(
        self,
        auth_token: str,
        *,
        project_id: str,
        agent_session_id: str,
        shot_no: str,
        decision: str,
        evidence_ref: str,
        expected_project_version: int,
    ) -> dict:
        """Persist a human problem-shot decision in existing task/event records."""
        shot = str(shot_no or "").strip()
        disposition = str(decision or "").strip()
        if not shot or not disposition:
            raise WorkspaceServiceError("INVALID_INPUT", "问题镜头确认需要镜号和人工结论")
        session = self.agent.get_session(auth_token, agent_session_id)["session"]
        if session["project_id"] != project_id:
            raise WorkspaceServiceError("PERMISSION_DENIED", "session 不属于当前项目")
        payload = {
            "action": "problem_shot_human_confirmation",
            "decision": disposition,
            "evidence_ref": evidence_ref,
            "project_version_before": expected_project_version,
            "shot_no": shot,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        task = self._readonly().services.tasks.create_task(
            auth_token,
            agent_session_id=agent_session_id,
            task_type="problem_shot_human_confirmation",
            payload_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            expected_project_version=expected_project_version,
        )
        understanding = self._readonly().services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="understanding",
            details=payload,
        )
        executing = self._readonly().services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=understanding["task_version"],
            status="executing",
            details=payload,
        )
        completed = self._readonly().services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=executing["task_version"],
            status="completed",
            details=payload,
            result_ref=evidence_ref,
        )
        project = self._readonly().services.projects.get_project(auth_token, project_id)
        return {
            "alpha": "执行中",
            "production_certification": "BLOCK",
            "task": completed,
            "actor_id": completed["actor_id"],
            "evidence_ref": evidence_ref,
            "project_version_before": expected_project_version,
            "project_version_after": project["project_version"],
            "publication": "not_created",
            "current": "not_written",
        }

    def confirm_whole_script(
        self,
        auth_token: str,
        *,
        project_id: str,
        version_id: str,
        evidence_ref: str,
        expected_project_version: int,
    ) -> dict:
        """Record an explicit human whole-script decision, never publication."""
        state = self.agent.get_project_state(auth_token, project_id)
        version_ids = {
            item["version_id"]
            for item in self._readonly().services.projects.list_project_versions(
                auth_token, project_id
            )
        }
        if version_id not in version_ids:
            raise WorkspaceServiceError("NOT_FOUND", "目标版本不属于当前项目")
        review = self._readonly().services.versions.record_review(
            auth_token,
            version_id=version_id,
            decision="quality_pass",
            evidence_ref=evidence_ref,
            expected_project_version=expected_project_version,
        )
        return {
            "alpha": "执行中",
            "production_certification": "BLOCK",
            "review": review,
            "publication": "not_created",
            "current": "not_written",
            "project_state_before_confirmation": state["blocker"]["code"],
        }

    def prepare_images(self, auth_token: str, *, project_id: str, script_rows: list[dict]) -> dict:
        return self._images().prepare_images(
            auth_token, project_id=project_id, script_rows=script_rows
        )

    def replace_image(self, auth_token: str, *, project_id: str, shot_no: str, script_rows: list[dict]) -> dict:
        return self._images().replace_image(
            auth_token, project_id=project_id, shot_no=shot_no, script_rows=script_rows
        )

    def create_image_delivery(self, auth_token: str, *, project_id: str, version_id: str, script_rows: list[dict], selections: list[dict]) -> dict:
        return self._images().create_delivery(
            auth_token, project_id=project_id, version_id=version_id,
            script_rows=script_rows, selections=selections,
        )

    def image_workbook(self, auth_token: str, *, project_id: str, delivery_sha256: str) -> Path:
        return self._images().workbook_path(
            auth_token, project_id=project_id, delivery_sha256=delivery_sha256
        )

    def _readonly(self) -> ReadonlyAgentService:
        return self.agent.services.readonly if isinstance(self.agent, ExecutableAgentService) else self.agent

    def _images(self) -> AlphaImageFlow:
        if self.image_flow is None:
            raise WorkspaceServiceError("ALPHA_IMAGE_UNAVAILABLE", "Alpha 图片接线尚未注入")
        return self.image_flow

    def _verified_brief(
        self,
        auth_token: str,
        *,
        project_id: str,
        brief_ref: str,
        expected_sha256: str,
    ) -> dict:
        if not project_id or not brief_ref or not _SHA256.fullmatch(str(expected_sha256 or "")):
            raise WorkspaceServiceError("BRIEF_REFERENCE_INVALID", "必须提供 project_id、Brief 引用及其预期 SHA-256")
        try:
            adapted = self.verified_brief_reader(project_id, brief_ref, expected_sha256)
        except Exception as exc:
            raise WorkspaceServiceError("VERIFIED_BRIEF_UNAVAILABLE", "已验证 Brief 读取失败，未创建项目或生成任务") from exc
        if not isinstance(adapted, dict) or not adapted:
            raise WorkspaceServiceError("VERIFIED_BRIEF_UNAVAILABLE", "已验证 Brief reader 未返回可用适配 Brief")
        return dict(adapted)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def create_alpha_app(console: AlphaConsole) -> FastAPI:
    """Dedicated Alpha API; deliberately separate from the shared employee nav."""
    app = FastAPI(title="M+ Alpha 项目中控台", docs_url=None, redoc_url=None)

    def auth(value: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> str:
        if not value.strip():
            raise HTTPException(status_code=401, detail={"code": "AUTH_SESSION_INVALID"})
        return value

    def failure(exc: Exception) -> HTTPException:
        return HTTPException(
            status_code=409,
            detail={
                "code": str(getattr(exc, "code", "ALPHA_CONSOLE_ERROR")),
                "message": str(getattr(exc, "message", "") or str(exc)),
            },
        )

    def flow() -> AlphaProjectFlow:
        if console.project_flow is None:
            raise WorkspaceServiceError("ALPHA_BRIEF_UNAVAILABLE", "Alpha 项目接线尚未注入")
        return console.project_flow

    async def object_payload(request: Request) -> dict:
        raw = await request.body()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WorkspaceServiceError("INVALID_INPUT", "请求体必须是 JSON object") from exc
        if not isinstance(payload, dict):
            raise WorkspaceServiceError("INVALID_INPUT", "请求体必须是 JSON object")
        return payload

    def reject_legacy_onboarding(payload: dict) -> None:
        if "brief" in payload or "confirmed" in payload:
            raise WorkspaceServiceError(
                "ALPHA_PROJECT_ONLY", "Alpha 不接受 raw brief 或 confirmed；请先绑定已确认 artifact"
            )

    def reject_caller_identity(payload: dict, field: str) -> None:
        if field in payload:
            raise WorkspaceServiceError(
                "ALPHA_ACTOR_FORGED", f"{field} 必须由认证会话在服务端确定"
            )

    @app.get("/api/alpha/status")
    def status() -> dict:
        return console.status()

    @app.get("/api/alpha/projects")
    def projects(workspace_id: str, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            return console.list_projects(auth(token), workspace_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects")
    async def create_project(request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        if console.project_flow is None:
            raise HTTPException(status_code=405, detail={"code": "METHOD_NOT_ALLOWED"})
        try:
            payload = await object_payload(request)
            reject_legacy_onboarding(payload)
            return flow().create_empty_project(
                auth(token),
                workspace_id=str(payload.get("workspace_id") or ""),
                name=str(payload.get("name") or ""),
                category_id=str(payload.get("category_id") or ""),
                expected_workspace_version=payload.get("expected_workspace_version"),
            )
        except (WorkspaceServiceError, TypeError, ValueError) as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects/{project_id}/brief-inputs/freeze")
    async def freeze_brief_inputs(project_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            payload = await object_payload(request)
            return flow().freeze_documents(
                auth(token), project_id=project_id, documents=payload.get("documents")
            )
        except (WorkspaceServiceError, TypeError, ValueError) as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects/{project_id}/brief-imports")
    async def import_brief(project_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            payload = await object_payload(request)
            return flow().import_documents(
                auth(token), project_id=project_id, documents=payload.get("documents")
            )
        except (WorkspaceServiceError, TypeError, ValueError) as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects/{project_id}/brief-conflicts/resolve")
    async def resolve_brief_conflicts(project_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            payload = await object_payload(request)
            reject_caller_identity(payload, "resolved_by")
            return flow().resolve_conflicts(
                auth(token),
                project_id=project_id,
                imported=payload.get("imported"),
                resolutions=payload.get("resolutions"),
            )
        except (WorkspaceServiceError, TypeError, ValueError) as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects/{project_id}/brief-confirmations")
    async def confirm_brief(project_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            payload = await object_payload(request)
            reject_legacy_onboarding(payload)
            reject_caller_identity(payload, "confirmed_by")
            return flow().confirm_and_bind(
                auth(token),
                project_id=project_id,
                imported=payload.get("imported"),
                expected_project_version=payload.get("expected_project_version"),
            )
        except (WorkspaceServiceError, TypeError, ValueError) as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects/{project_id}/enter")
    async def enter_project(project_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            payload = await object_payload(request)
            if console.project_flow is not None and payload:
                raise WorkspaceServiceError("ALPHA_PROJECT_ONLY", "进入 Alpha 项目只能提供 project_id")
            return console.enter_project(
                auth(token),
                project_id=project_id,
                brief_ref=str(payload.get("brief_ref") or ""),
                expected_sha256=str(payload.get("expected_sha256") or ""),
            )
        except (WorkspaceServiceError, TypeError, ValueError) as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects/{project_id}/sessions")
    async def create_session(project_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            payload = await object_payload(request)
            if console.project_flow is not None and payload:
                raise WorkspaceServiceError("ALPHA_PROJECT_ONLY", "创建 Alpha session 只能提供 project_id")
            return console.create_session(
                auth(token), project_id=project_id,
                brief_ref=str(payload.get("brief_ref") or ""),
                expected_sha256=str(payload.get("expected_sha256") or ""),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/alpha/projects/{project_id}")
    def dashboard(project_id: str, agent_session_id: str = "", token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            return console.project_dashboard(
                auth(token), project_id=project_id, agent_session_id=agent_session_id or None
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/alpha/projects/{project_id}/problem-shots")
    def problem_shots(project_id: str, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            return console.problem_shots(auth(token), project_id=project_id)
        except (WorkspaceServiceError, OSError, json.JSONDecodeError) as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/sessions/{agent_session_id}/messages")
    async def message(agent_session_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        payload = await request.json()
        try:
            return console.submit_message(
                auth(token),
                agent_session_id=agent_session_id,
                content=str(payload.get("content") or ""),
                run_id=payload.get("run_id"),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/tasks/{task_id}/confirm")
    async def confirm_low_risk_action(task_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        payload = await request.json()
        try:
            return console.confirm_low_risk_action(
                auth(token), task_id=task_id,
                proposal_digest=str(payload.get("proposal_digest") or ""),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects/{project_id}/problem-shot-confirmations")
    async def problem_shot_confirmation(project_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        payload = await request.json()
        try:
            return console.confirm_problem_shot(
                auth(token),
                project_id=project_id,
                agent_session_id=str(payload.get("agent_session_id") or ""),
                shot_no=str(payload.get("shot_no") or ""),
                decision=str(payload.get("decision") or ""),
                evidence_ref=str(payload.get("evidence_ref") or ""),
                expected_project_version=payload.get("expected_project_version"),
            )
        except (WorkspaceServiceError, TypeError, ValueError) as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects/{project_id}/whole-script-confirmations")
    async def whole_script_confirmation(project_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        payload = await request.json()
        try:
            return console.confirm_whole_script(
                auth(token),
                project_id=project_id,
                version_id=str(payload.get("version_id") or ""),
                evidence_ref=str(payload.get("evidence_ref") or ""),
                expected_project_version=payload.get("expected_project_version"),
            )
        except (WorkspaceServiceError, TypeError, ValueError) as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects/{project_id}/images/prepare")
    async def prepare_images(project_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            payload = await object_payload(request)
            return console.prepare_images(auth(token), project_id=project_id, script_rows=payload.get("script_rows"))
        except (WorkspaceServiceError, TypeError, ValueError) as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects/{project_id}/images/{shot_no}/replace")
    async def replace_image(project_id: str, shot_no: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            payload = await object_payload(request)
            return console.replace_image(auth(token), project_id=project_id, shot_no=shot_no, script_rows=payload.get("script_rows"))
        except (WorkspaceServiceError, TypeError, ValueError) as exc:
            raise failure(exc) from exc

    @app.post("/api/alpha/projects/{project_id}/image-deliveries")
    async def create_image_delivery(project_id: str, request: Request, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            payload = await object_payload(request)
            return console.create_image_delivery(
                auth(token), project_id=project_id,
                version_id=str(payload.get("version_id") or ""),
                script_rows=payload.get("script_rows"), selections=payload.get("selections"),
            )
        except (WorkspaceServiceError, TypeError, ValueError) as exc:
            raise failure(exc) from exc

    @app.get("/api/alpha/projects/{project_id}/image-deliveries/{delivery_sha256}/workbook")
    def download_image_delivery(project_id: str, delivery_sha256: str, token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> FileResponse:
        try:
            path = console.image_workbook(auth(token), project_id=project_id, delivery_sha256=delivery_sha256)
            return FileResponse(path, filename=path.name, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(Path(__file__).with_name("ui") / "alpha.html")

    return app
