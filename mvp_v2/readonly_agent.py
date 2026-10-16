from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from .artifact_service import ArtifactResolutionError, RunArtifactResolver
from .identity_service import LocalIdentityAdapter
from .project_service import ProjectService
from .runtime_config import load_runtime_config
from .task_service import TaskService
from .version_service import VersionService
from .workspace_store import WorkspaceServiceError, WorkspaceStore


_EXECUTION_TERMS = re.compile(
    r"(开始|执行|生成|修改|重试|取消|发布|导入|创建|替换|删除|更新|提交审核)"
)
_BLOCKER_TERMS = re.compile(r"(为什么|阻断|卡住|停住|失败|不能|原因|怎么办)"
)
_RUN_DETAIL_TERMS = re.compile(r"(节点|run|运行详情|产物|进度详情)", re.IGNORECASE)
_STATE_TERMS = re.compile(r"(现在.*(到哪|状态)|到哪|当前状态|项目状态|进度)")


@dataclass(frozen=True)
class ReadonlyAgentServices:
    store: WorkspaceStore
    identity: LocalIdentityAdapter
    projects: ProjectService
    tasks: TaskService
    versions: VersionService
    artifacts: RunArtifactResolver


class ReadonlyAgentService:
    """E02 deterministic, read-only agent boundary.

    The only writes are agent session/task/event audit records. It never calls
    the pipeline or changes project, version, review, publication or assets.
    """

    def __init__(self, services: ReadonlyAgentServices):
        self.services = services

    @classmethod
    def from_runtime_config(cls, config: dict | None = None) -> "ReadonlyAgentService":
        runtime = config or load_runtime_config()
        store = WorkspaceStore.from_runtime_config(runtime)
        return cls(
            ReadonlyAgentServices(
                store=store,
                identity=LocalIdentityAdapter(store),
                projects=ProjectService(store),
                tasks=TaskService(store),
                versions=VersionService(store),
                artifacts=RunArtifactResolver(runtime["runs_root"]),
            )
        )

    def setup_status(self) -> dict:
        with self.services.store.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM workspaces) AS workspace_count,
                    (SELECT COUNT(*) FROM actors
                     WHERE auth_source = 'local' AND status = 'active') AS local_actor_count
                """
            ).fetchone()
        return {
            "enabled": bool(row["workspace_count"] and row["local_actor_count"]),
            "mode": "local_owner",
            "requires_explicit_confirmation": True,
        }

    def enable_local_owner(
        self,
        *,
        workspace_name: str,
        display_name: str,
        confirmed: bool,
    ) -> dict:
        return self.services.identity.bootstrap_owner(
            workspace_name=workspace_name,
            display_name=display_name,
            confirmed=confirmed,
        )

    def reissue_local_owner_session(self, *, confirmed: bool) -> dict:
        return self.services.identity.reissue_local_owner_session(
            confirmed=confirmed,
        )

    def list_projects(self, auth_token: str, workspace_id: str) -> list[dict]:
        return [
            {
                "project_id": item["project_id"],
                "name": item["name"],
                "category_id": item["category_id"],
                "status": item["status"],
                "project_version": item["project_version"],
                "has_brief": bool(item.get("current_brief_snapshot_id")),
            }
            for item in self.services.projects.list_projects(auth_token, workspace_id)
        ]

    def create_session(self, auth_token: str, project_id: str) -> dict:
        session = self.services.tasks.create_agent_session(
            auth_token, project_id=project_id
        )
        return self._session_response(auth_token, session["agent_session_id"])

    def get_session(self, auth_token: str, agent_session_id: str) -> dict:
        return self._session_response(auth_token, agent_session_id)

    def list_task_events(
        self,
        auth_token: str,
        task_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[dict]:
        return self.services.tasks.list_task_events(
            auth_token, task_id, after_sequence=after_sequence
        )

    def _session_response(self, auth_token: str, agent_session_id: str) -> dict:
        session = self.services.tasks.get_agent_session(auth_token, agent_session_id)
        tasks = self.services.tasks.list_agent_session_tasks(
            auth_token, agent_session_id
        )
        timeline = []
        for task in tasks:
            for event in self.services.tasks.list_task_events(
                auth_token, task["task_id"], detail_paths=('response',),
                latest_with_detail='response' if task['task_type'] == 'formal_image_candidates' else None,
            ):
                response = event.get("details", {}).get("response")
                if response:
                    timeline.append(
                        {
                            "task_id": task["task_id"],
                            "task_type": task["task_type"],
                            "sequence_no": event["sequence_no"],
                            "status": event["status"],
                            "created_at": event["created_at"],
                            "response": response,
                        }
                    )
        return {
            "session": session,
            "tasks": tasks,
            "timeline": timeline,
            "current_task": tasks[-1] if tasks else None,
        }

    def submit_message(
        self,
        auth_token: str,
        agent_session_id: str,
        *,
        content: str,
        run_id: str | None = None,
    ) -> dict:
        message = str(content or "").strip()
        if not message:
            raise WorkspaceServiceError("INVALID_INPUT", "message 不能为空")
        session = self.services.tasks.get_agent_session(auth_token, agent_session_id)
        project = self.services.projects.get_project(
            auth_token, session["project_id"]
        )
        intent = self._route_intent(message)
        task = self.services.tasks.create_task(
            auth_token,
            agent_session_id=agent_session_id,
            task_type=intent,
            payload_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
            expected_project_version=project["project_version"],
        )

        if intent == "execution_rejected":
            response = {
                "type": "readonly_rejection",
                "title": "E02 不可执行",
                "summary": "当前助手只读取和解释真实项目状态，不会执行生成、修改、重试、导入或发布。",
                "reason": "可执行闭环属于 E03，必须先展示影响范围并通过确认门。",
                "next_steps": ["可询问当前状态", "可询问阻断原因", "可查看运行节点详情"],
            }
            finished = self.services.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=task["task_version"],
                status="blocked",
                details={"intent": intent, "response": response},
                error_code="E02_READ_ONLY",
            )
            return self._message_response(auth_token, finished, response)

        executing = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="executing",
            details={"intent": intent},
        )
        try:
            response = self._read_response(
                auth_token,
                project_id=session["project_id"],
                intent=intent,
                run_id=run_id,
            )
        except (ArtifactResolutionError, WorkspaceServiceError) as exc:
            if isinstance(exc, ArtifactResolutionError):
                response = self._integrity_blocker(exc.code)
            else:
                response = self._read_error_blocker(exc.code)
            finished = self.services.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=executing["task_version"],
                status="blocked",
                details={"intent": intent, "response": response},
                error_code=exc.code,
            )
        else:
            finished = self.services.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=executing["task_version"],
                status="completed",
                details={"intent": intent, "response": response},
            )
        return self._message_response(auth_token, finished, response)

    def _message_response(self, auth_token: str, task: dict, response: dict) -> dict:
        return {
            "task": task,
            "response": response,
            "events": self.services.tasks.list_task_events(
                auth_token, task["task_id"]
            ),
        }

    @staticmethod
    def _route_intent(message: str) -> str:
        if _BLOCKER_TERMS.search(message):
            return "explain_blocker"
        if _RUN_DETAIL_TERMS.search(message):
            return "get_run_details"
        if _STATE_TERMS.search(message):
            return "get_project_state"
        if _EXECUTION_TERMS.search(message):
            return "execution_rejected"
        return "get_project_state"

    def _read_response(
        self,
        auth_token: str,
        *,
        project_id: str,
        intent: str,
        run_id: str | None,
    ) -> dict:
        if intent == "get_project_state":
            return self.get_project_state(auth_token, project_id)

        target_run_id = str(run_id or "").strip()
        if not target_run_id:
            runs = self.services.projects.list_project_runs(auth_token, project_id)
            target_run_id = str(runs[-1]["run_id"]) if runs else ""
        if not target_run_id:
            return self.get_project_state(auth_token, project_id)

        details = self.get_run_details(auth_token, target_run_id)
        if details["project_id"] != project_id:
            raise WorkspaceServiceError("PERMISSION_DENIED", "run 不属于当前项目")
        if intent == "explain_blocker":
            return {
                "type": "blocker_explanation",
                "title": details["blocker"]["title"],
                "summary": details["blocker"]["detail"],
                "run_id": target_run_id,
                "blocker": details["blocker"],
                "next_steps": [details["blocker"]["next_step"]],
            }
        return details

    def get_project_state(self, auth_token: str, project_id: str) -> dict:
        project = self.services.projects.get_project(auth_token, project_id)
        brief = self.services.projects.get_current_brief_snapshot(auth_token, project_id)
        runs = self.services.projects.list_project_runs(auth_token, project_id)
        versions = self.services.projects.list_project_versions(auth_token, project_id)

        latest_run = None
        run_integrity_error = None
        if runs:
            try:
                latest_run = self.get_run_details(auth_token, runs[-1]["run_id"])
            except ArtifactResolutionError as exc:
                run_integrity_error = self._integrity_blocker(exc.code)

        version_state = self._version_state(auth_token, versions)
        if brief is None:
            blocker = {
                "code": "BRIEF_REQUIRED",
                "title": "缺少 Brief",
                "detail": "当前项目还没有已登记的 Brief 快照，无法判断生成输入。",
                "next_step": "先为项目导入并登记 Brief。",
                "is_technical_failure": False,
            }
        elif not runs:
            blocker = {
                "code": "RUN_REQUIRED",
                "title": "尚无运行版本",
                "detail": "Brief 已登记，但当前项目还没有绑定的真实 run。",
                "next_step": "E03 启用后可提交生成；E02 只提供状态解释。",
                "is_technical_failure": False,
            }
        elif run_integrity_error:
            blocker = run_integrity_error["blocker"]
        else:
            blocker = latest_run["blocker"]
            latest_review = version_state["latest_review"]
            if latest_review and blocker["code"] == "WAITING_HUMAN_REVIEW":
                if latest_review["decision"] == "quality_pass":
                    blocker = {
                        "code": "HUMAN_REVIEW_PASSED",
                        "title": "人工审核已通过",
                        "detail": "最新版本已有独立的人工 quality_pass 记录；这不等于已经正式发布。",
                        "next_step": "查看候选与发布记录，确认后续受控动作。",
                        "is_technical_failure": False,
                    }
                else:
                    blocker = {
                        "code": "HUMAN_REVIEW_FAILED",
                        "title": "人工审核未通过",
                        "detail": "最新人工审核结论为 quality_fail，当前版本不具备发布资格。",
                        "next_step": "根据人工结论提出修改；E02 不执行修改。",
                        "is_technical_failure": False,
                    }

        return {
            "type": "project_state",
            "title": project["name"],
            "summary": blocker["detail"],
            "project": {
                "project_id": project["project_id"],
                "name": project["name"],
                "category_id": project["category_id"],
                "status": project["status"],
                "project_version": project["project_version"],
            },
            "brief": {
                "available": brief is not None,
                "snapshot_id": brief["brief_snapshot_id"] if brief else None,
                "sha256": brief["brief_sha256"] if brief else None,
            },
            "run": latest_run,
            "versions": version_state,
            "blocker": blocker,
            "next_steps": [blocker["next_step"]],
        }

    def get_run_details(self, auth_token: str, run_id: str) -> dict:
        binding = self.services.projects.get_run_binding(auth_token, run_id)
        manifest = self.services.artifacts.read_manifest(
            run_id,
            expected_sha256=binding["manifest_sha256"],
        )
        stages = []
        for stage_id, record in (manifest.get("stages") or {}).items():
            error = record.get("error") if isinstance(record, dict) else None
            stages.append(
                {
                    "stage_id": stage_id,
                    "status": str((record or {}).get("status") or "not_started"),
                    "attempt": int((record or {}).get("attempt") or 0),
                    "error": (
                        {
                            "code": str(error.get("code") or "UNKNOWN"),
                            "category": str(error.get("category") or "unknown"),
                            "retryable": bool(error.get("retryable")),
                        }
                        if isinstance(error, dict)
                        else None
                    ),
                }
            )

        verified_artifacts = []
        artifact_errors = []
        for artifact_name in sorted((manifest.get("artifacts") or {})):
            try:
                artifact = self.services.artifacts.resolve_artifact(
                    run_id, artifact_name
                )
            except ArtifactResolutionError as exc:
                artifact_errors.append(
                    {"artifact_name": artifact_name, "error_code": exc.code}
                )
            else:
                verified_artifacts.append(
                    {
                        "artifact_name": artifact_name,
                        "ref": artifact.ref,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                    }
                )

        blocker = self._run_blocker(
            manifest_status=str(manifest.get("status") or "unknown"),
            stages=stages,
            artifact_errors=artifact_errors,
        )
        active_stage = next(
            (
                stage
                for stage in stages
                if stage["status"]
                in {"running", "blocked", "failed", "waiting_human_review"}
            ),
            None,
        )
        return {
            "type": "run_details",
            "title": "运行节点详情",
            "summary": blocker["detail"],
            "run_id": run_id,
            "project_id": binding["project_id"],
            "parent_run_id": binding.get("parent_run_id"),
            "status": str(manifest.get("status") or "unknown"),
            "category_id": str(manifest.get("category_id") or ""),
            "active_stage": active_stage,
            "stages": stages,
            "verified_artifacts": verified_artifacts,
            "artifact_integrity_errors": artifact_errors,
            "blocker": blocker,
            "next_steps": [blocker["next_step"]],
        }

    def explain_blocker(self, auth_token: str, run_id: str) -> dict:
        details = self.get_run_details(auth_token, run_id)
        return {
            "type": "blocker_explanation",
            "title": details["blocker"]["title"],
            "summary": details["blocker"]["detail"],
            "run_id": run_id,
            "blocker": details["blocker"],
            "next_steps": [details["blocker"]["next_step"]],
        }

    def _version_state(self, auth_token: str, versions: list[dict]) -> dict:
        reviews = []
        candidates = []
        publications = []
        for version in versions:
            version_id = version["version_id"]
            reviews.extend(self.services.versions.list_reviews(auth_token, version_id))
            candidates.extend(
                self.services.versions.list_candidates(auth_token, version_id)
            )
            publications.extend(
                self.services.versions.list_publications(auth_token, version_id)
            )
        latest = versions[-1] if versions else None
        latest_review = max(reviews, key=lambda item: item["created_at"]) if reviews else None
        latest_candidate = (
            max(candidates, key=lambda item: item["created_at"]) if candidates else None
        )
        latest_publication = (
            max(publications, key=lambda item: item["created_at"])
            if publications
            else None
        )
        return {
            "count": len(versions),
            "latest_version_id": latest["version_id"] if latest else None,
            "candidate_version_id": (
                latest_candidate["version_id"] if latest_candidate else None
            ),
            "published_version_id": (
                latest_publication["version_id"] if latest_publication else None
            ),
            "latest_review": (
                {
                    "version_id": latest_review["version_id"],
                    "decision": latest_review["decision"],
                    "created_at": latest_review["created_at"],
                }
                if latest_review
                else None
            ),
        }

    @staticmethod
    def _run_blocker(
        *,
        manifest_status: str,
        stages: list[dict],
        artifact_errors: list[dict],
    ) -> dict:
        if artifact_errors:
            return {
                "code": "ARTIFACT_INTEGRITY_BLOCKED",
                "title": "产物完整性校验未通过",
                "detail": "至少一个运行产物与 manifest 中登记的 SHA-256 不一致或不可读取。",
                "next_step": "停止使用该 run，并由管理员核对不可变产物。",
                "is_technical_failure": True,
            }
        failing = next(
            (stage for stage in stages if stage["status"] in {"blocked", "failed"}),
            None,
        )
        if failing:
            technical = failing["status"] == "failed"
            error_code = (failing.get("error") or {}).get("code") or "RUN_BLOCKED"
            return {
                "code": error_code,
                "title": "技术失败" if technical else "内容或合同条件阻断",
                "detail": f"运行停在 {failing['stage_id']} 节点；未读取或展示原始 Brief 和本机路径。",
                "next_step": (
                    "由管理员查看受保护日志后决定是否重试。"
                    if technical
                    else "补齐输入或审核条件后，再由 E03 提交受控重试。"
                ),
                "is_technical_failure": technical,
            }
        waiting = next(
            (stage for stage in stages if stage["status"] == "waiting_human_review"),
            None,
        )
        if waiting or manifest_status == "waiting_human_review":
            return {
                "code": "WAITING_HUMAN_REVIEW",
                "title": "等待人工审核",
                "detail": "机器流程已到人工审核门；这不是技术失败，也不代表质量已经通过。",
                "next_step": "由有权限的审核人完成真实脚本文档审核。",
                "is_technical_failure": False,
            }
        running = next(
            (stage for stage in stages if stage["status"] == "running"), None
        )
        if running:
            return {
                "code": "RUNNING",
                "title": "正在运行",
                "detail": f"当前正在执行 {running['stage_id']} 节点。",
                "next_step": "等待节点产生新的持久状态。",
                "is_technical_failure": False,
            }
        return {
            "code": "NO_ACTIVE_BLOCKER",
            "title": "未发现活动阻断",
            "detail": "当前 manifest 未记录运行中、阻断、失败或等待人工审核节点。",
            "next_step": "查看版本与审核记录，确认后续业务动作。",
            "is_technical_failure": False,
        }

    @staticmethod
    def _integrity_blocker(error_code: str) -> dict:
        blocker = {
            "code": error_code,
            "title": "运行证据完整性阻断",
            "detail": "绑定的 run manifest 未通过 SHA-256 或作用域校验，助手拒绝继续解释该运行。",
            "next_step": "由管理员核对 run 绑定与不可变 manifest。",
            "is_technical_failure": True,
        }
        return {
            "type": "blocker_explanation",
            "title": blocker["title"],
            "summary": blocker["detail"],
            "blocker": blocker,
            "next_steps": [blocker["next_step"]],
        }

    @staticmethod
    def _read_error_blocker(error_code: str) -> dict:
        blocker = {
            "code": error_code,
            "title": "只读查询被阻断",
            "detail": "当前查询不属于本会话项目，或目标记录不存在；助手没有执行任何项目动作。",
            "next_step": "选择当前项目中已登记的 run 后重新查询。",
            "is_technical_failure": False,
        }
        return {
            "type": "blocker_explanation",
            "title": blocker["title"],
            "summary": blocker["detail"],
            "blocker": blocker,
            "next_steps": [blocker["next_step"]],
        }
