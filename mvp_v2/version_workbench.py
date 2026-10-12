from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable

from .action_confirmation import ManualPageConfirmationService
from .manual_actions import MANUAL_ACTION_REGISTRY, validate_manual_action_input
from .identity_service import require_permission
from .artifact_service import (
    ArtifactResolutionError,
    ResolvedArtifact,
    RunArtifactResolver,
)
from .project_service import ProjectService
from .portable_paths import resolve_run_local_path
from .revision_service import (
    build_revision_report as build_domain_revision_report,
    compare_versions as compare_domain_versions,
    version_shots_from_artifacts,
)
from .runtime_config import load_runtime_config
from .task_service import TaskService
from .tool_idempotency import ManualPageIdempotencyService
from .version_service import VersionService
from .version_delivery import (
    VersionDeliveryArtifact,
    VersionDeliveryArtifacts,
    VersionDeliveryContext,
    resolve_version_shot_ids,
)
from .workspace_store import WorkspaceServiceError, WorkspaceStore


_EXPECTED_SCHEMAS = {
    "script_plan": {"mplus-script-plan/v1", "mplus-script-plan/v2", "mplus-script-plan/v3", "mplus-script-plan/v4"},
    "script_draft": {"mplus-script-draft/v1"},
    "visual_binding": {"mplus-visual-binding/v1"},
}
_VERSION_WRITE_ACTIONS = {
    "record_version_review",
    "select_candidate_version",
    "publish_version",
}
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class VersionWorkbenchRecoveryRequired(RuntimeError):
    code = "IDEMPOTENCY_RECOVERY_REQUIRED"

    def __init__(self, *, tool_name: str, idempotency_record_id: str):
        self.tool_name = tool_name
        self.idempotency_record_id = idempotency_record_id
        self.retryable = False
        self.recovery = {
            "automatic_retry": False,
            "reason": "受控动作可能已进入领域写入窗口，不能安全判断是否应再次执行",
            "required_checks": [
                "核对 candidate_selections 或 version_publications 是否已有目标记录",
                "核对对应 task 与 outbox 后，由管理员决定补记结果或换新幂等键重试",
            ],
        }
        super().__init__(
            f"{self.code}: {tool_name} 结果不确定，已停止自动重放"
        )


@dataclass(frozen=True)
class VersionWorkbenchServices:
    store: WorkspaceStore
    projects: ProjectService
    versions: VersionService
    tasks: TaskService
    artifacts: RunArtifactResolver
    confirmations: ManualPageConfirmationService
    idempotency: ManualPageIdempotencyService


class VersionWorkbenchService:
    """Verified E04 version reads plus controlled candidate/publication writes.

    Confirmation, idempotency, task audit and VersionService intentionally keep
    their existing transaction boundaries. Therefore an interrupted in-progress
    action is never executed again automatically; it is surfaced for operator
    reconciliation instead of being described as atomic.
    """

    def __init__(
        self,
        services: VersionWorkbenchServices,
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
    ) -> "VersionWorkbenchService":
        runtime = config or load_runtime_config()
        store = WorkspaceStore.from_runtime_config(runtime)
        return cls(
            VersionWorkbenchServices(
                store=store,
                projects=ProjectService(store),
                versions=VersionService(store),
                tasks=TaskService(store),
                artifacts=RunArtifactResolver(runtime["runs_root"]),
                confirmations=ManualPageConfirmationService(
                    store, confirmation_signing_key
                ),
                idempotency=ManualPageIdempotencyService(store),
            )
        )

    def list_version_tree(self, auth_token: str, project_id: str) -> dict:
        project = self.services.projects.get_project(auth_token, project_id)
        versions = self.services.projects.list_project_versions(
            auth_token, project_id
        )
        version_no_by_id = {
            version["version_id"]: index
            for index, version in enumerate(versions, start=1)
        }
        governance = {
            version["version_id"]: self._version_governance(
                auth_token, version["version_id"]
            )
            for version in versions
        }
        current_candidate_id = self._current_candidate_id(project_id)
        by_id = {item["version_id"]: item for item in versions}
        children: dict[str | None, list[dict]] = {}
        for version in versions:
            parent_id = version.get("parent_version_id")
            if parent_id is not None and parent_id not in by_id:
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED",
                    "version tree 包含不属于当前 project 的 parent version",
                )
            children.setdefault(parent_id, []).append(version)

        def node(version: dict) -> dict:
            return {
                "version_id": version["version_id"],
                "version_no": version_no_by_id[version["version_id"]],
                "project_id": project_id,
                "run_id": version["run_id"],
                "parent_version_id": version.get("parent_version_id"),
                "content_sha256": version["content_sha256"],
                "artifact_ref": version["artifact_ref"],
                "created_at": version["created_at"],
                "is_current": version["version_id"] == project.get("current_version_id"),
                "read_only": version["version_id"] != project.get("current_version_id"),
                "governance": {
                    **governance[version["version_id"]],
                    "is_current_candidate": (
                        current_candidate_id is not None
                        and (
                            governance[version["version_id"]].get(
                                "latest_candidate"
                            )
                            or {}
                        ).get("candidate_id")
                            == current_candidate_id
                    ),
                },
                "children": [
                    node(child)
                    for child in sorted(
                        children.get(version["version_id"], []),
                        key=lambda item: (item["created_at"], item["version_id"]),
                    )
                ],
            }

        roots = [
            node(item)
            for item in sorted(
                children.get(None, []),
                key=lambda item: (item["created_at"], item["version_id"]),
            )
        ]
        allowed_actions = []
        with self.services.store.connection() as connection:
            for action in sorted(_VERSION_WRITE_ACTIONS):
                try:
                    require_permission(connection, auth_token, workspace_id=project["workspace_id"],
                                       project_id=project_id, permission=MANUAL_ACTION_REGISTRY[action].permission)
                except WorkspaceServiceError as exc:
                    if exc.code != "PERMISSION_DENIED":
                        raise
                else:
                    allowed_actions.append(action)
        return {
            "project_id": project_id,
            "allowed_actions": allowed_actions,
            "project_version": project["project_version"],
            "version_count": len(versions),
            "roots": roots,
        }

    def read_version(
        self,
        auth_token: str,
        *,
        project_id: str,
        version_id: str,
    ) -> dict:
        project = self.services.projects.get_project(auth_token, project_id)
        versions = self.services.projects.list_project_versions(auth_token, project_id)
        version_no_by_id = {
            version["version_id"]: index
            for index, version in enumerate(versions, start=1)
        }
        if version_id not in version_no_by_id:
            raise WorkspaceServiceError("NOT_FOUND", "版本不存在或不属于当前项目")
        resolved = self._version_artifacts(
            auth_token, project_id=project_id, version_id=version_id,
        )
        return {
            "project_id": project_id,
            "version_id": version_id,
            "version_no": version_no_by_id[version_id],
            "is_current": version_id == project.get("current_version_id"),
            "read_only": True,
            "verified": True,
            "shots": resolved["shots"],
        }

    def compare_versions(
        self,
        auth_token: str,
        *,
        project_id: str,
        left_version_id: str,
        right_version_id: str,
    ) -> dict:
        left = self._version_artifacts(
            auth_token, project_id=project_id, version_id=left_version_id
        )
        right = self._version_artifacts(
            auth_token, project_id=project_id, version_id=right_version_id
        )
        if left["has_frozen_lineage"] or right["has_frozen_lineage"]:
            for resolved in (left, right):
                for shot in resolved["shots"]:
                    shot["shot_id"] = resolved["shot_ids"][shot["shot_no"]]
        diff = compare_domain_versions(
            project_id=project_id,
            parent_version_id=left_version_id,
            child_version_id=right_version_id,
            parent_shots=left["shots"],
            child_shots=right["shots"],
        )
        diff["verified_artifacts"] = {
            "left": left["evidence"],
            "right": right["evidence"],
        }
        return _redact_absolute_paths(diff)

    def build_revision_report(
        self,
        auth_token: str,
        *,
        project_id: str,
        parent_version_id: str,
        child_version_id: str,
        original_feedback: str,
        structured_intent: dict,
        expected_impact: dict,
        locked_fields: list[dict],
        rerun_nodes: list[str],
        unresolved_items: list,
        machine_review: dict,
        human_review: dict,
        publication: dict,
    ) -> dict:
        diff = self.compare_versions(
            auth_token,
            project_id=project_id,
            left_version_id=parent_version_id,
            right_version_id=child_version_id,
        )
        report = build_domain_revision_report(
            diff=diff,
            original_feedback=original_feedback,
            structured_intent=structured_intent,
            expected_impact=expected_impact,
            locked_fields=locked_fields,
            rerun_nodes=rerun_nodes,
            unresolved_items=unresolved_items,
            machine_review=machine_review,
            human_review=human_review,
            publication=publication,
        )
        return _redact_absolute_paths(report)

    def issue_confirmation(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
    ) -> dict:
        if tool_name not in _VERSION_WRITE_ACTIONS:
            raise WorkspaceServiceError("INVALID_INPUT", "未知版本治理动作")
        normalized = dict(parameters or {})
        normalized.pop("confirmation_token", None)
        try:
            validated = validate_manual_action_input(
                tool_name,
                {
                    **normalized,
                    "confirmation_token": "confirmation-placeholder-00000000",
                },
            ).model_dump(mode="json")
        except (TypeError, ValueError) as exc:
            raise WorkspaceServiceError(
                "INVALID_INPUT", "版本治理动作参数不符合公共合同"
            ) from exc
        validated.pop("confirmation_token", None)
        normalized = validated
        self._require_action_authority(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=normalized,
        )
        return self._manual_confirmations().issue(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=normalized,
        )

    def record_version_review(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        project_id: str,
        version_id: str,
        expected_project_version: int,
        idempotency_key: str,
        confirmation_token: str,
    ) -> dict:
        self._require_project_version(
            auth_token, project_id=project_id, version_id=version_id
        )
        parameters = {
            "project_id": project_id,
            "version_id": version_id,
            "expected_project_version": expected_project_version,
            "idempotency_key": idempotency_key,
            "confirmation_token": confirmation_token,
        }

        def record_verified_review() -> dict:
            evidence = self._verified_review_evidence(
                auth_token,
                project_id=project_id,
                version_id=version_id,
            )
            return self.services.versions.record_review(
                auth_token,
                version_id=version_id,
                decision=evidence["decision"],
                evidence_ref=evidence["evidence_ref"],
                expected_project_version=expected_project_version,
            )

        return self._controlled_write(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name="record_version_review",
            parameters=parameters,
            authority=lambda: self._require_action_authority(
                auth_token,
                agent_session_id=agent_session_id,
                tool_name="record_version_review",
                parameters=parameters,
            ),
            action=record_verified_review,
            result_ref=lambda result: (
                f"artifact://workspace/project/{project_id}/review/"
                f"{result['review_id']}"
            ),
        )

    def select_candidate_version(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        project_id: str,
        version_id: str,
        expected_project_version: int,
        idempotency_key: str,
        confirmation_token: str,
    ) -> dict:
        self._require_project_version(
            auth_token, project_id=project_id, version_id=version_id
        )
        parameters = {
            "project_id": project_id,
            "version_id": version_id,
            "expected_project_version": expected_project_version,
            "idempotency_key": idempotency_key,
            "confirmation_token": confirmation_token,
        }
        return self._controlled_write(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name="select_candidate_version",
            parameters=parameters,
            authority=lambda: self._require_action_authority(
                auth_token,
                agent_session_id=agent_session_id,
                tool_name="select_candidate_version",
                parameters=parameters,
            ),
            action=lambda: self.services.versions.select_candidate(
                auth_token,
                version_id=version_id,
                expected_project_version=expected_project_version,
                reason="Agent 受控动作确认",
            ),
            result_ref=lambda result: (
                f"artifact://workspace/project/{project_id}/candidate/"
                f"{result['candidate_id']}"
            ),
        )

    def publish_version(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        project_id: str,
        version_id: str,
        review_id: str,
        expected_project_version: int,
        idempotency_key: str,
        confirmation_token: str,
    ) -> dict:
        self._require_project_version(
            auth_token, project_id=project_id, version_id=version_id
        )
        parameters = {
            "project_id": project_id,
            "version_id": version_id,
            "review_id": review_id,
            "expected_project_version": expected_project_version,
            "idempotency_key": idempotency_key,
            "confirmation_token": confirmation_token,
        }
        return self._controlled_write(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name="publish_version",
            parameters=parameters,
            authority=lambda: self._require_action_authority(
                auth_token,
                agent_session_id=agent_session_id,
                tool_name="publish_version",
                parameters=parameters,
            ),
            action=lambda: self.services.versions.publish_version(
                auth_token,
                version_id=version_id,
                review_id=review_id,
                expected_project_version=expected_project_version,
            ),
            result_ref=lambda result: (
                f"artifact://workspace/project/{project_id}/publication/"
                f"{result['publication_id']}"
            ),
        )

    def _require_action_authority(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
    ) -> dict:
        session = self.services.tasks.get_agent_session(
            auth_token, agent_session_id
        )
        if session["status"] != "active":
            raise WorkspaceServiceError("NOT_FOUND", "active agent session 不存在")
        project_id = str(parameters.get("project_id") or "")
        version_id = str(parameters.get("version_id") or "")
        if session["project_id"] != project_id:
            raise WorkspaceServiceError(
                "PERMISSION_DENIED", "版本治理动作与 agent session 项目不一致"
            )
        version = self._require_project_version(
            auth_token, project_id=project_id, version_id=version_id
        )
        project = self.services.projects.get_project(auth_token, project_id)
        expected = parameters.get("expected_project_version")
        if type(expected) is not int or expected < 0:
            raise WorkspaceServiceError(
                "INVALID_INPUT", "expected_project_version 必须是非负整数"
            )
        if project["project_version"] != expected:
            raise WorkspaceServiceError(
                "PROJECT_VERSION_CONFLICT", "project 已变化，请刷新版本工作台后重新确认"
            )
        if tool_name == "record_version_review":
            self._verified_review_evidence(
                auth_token,
                project_id=project_id,
                version_id=version_id,
            )
        elif tool_name == "publish_version":
            self._require_publishable(
                auth_token,
                project_id=project_id,
                version_id=version_id,
                review_id=str(parameters.get("review_id") or ""),
            )
        elif tool_name != "select_candidate_version":
            raise WorkspaceServiceError("INVALID_INPUT", "未知版本治理动作")
        return {"project": project, "version": version, "session": session}

    def _verified_review_evidence(
        self,
        auth_token: str,
        *,
        project_id: str,
        version_id: str,
    ) -> dict:
        version = self._require_project_version(
            auth_token, project_id=project_id, version_id=version_id
        )
        binding = self.services.projects.get_run_binding(
            auth_token, version["run_id"]
        )
        if binding["project_id"] != project_id:
            raise WorkspaceServiceError(
                "PERMISSION_DENIED", "version run 不属于当前 project"
            )
        manifest = self.services.artifacts.read_manifest(
            version["run_id"], expected_sha256=binding["manifest_sha256"]
        )
        bound_manifest = self.services.artifacts.resolve_manifest(
            version["run_id"], expected_sha256=binding["manifest_sha256"]
        )
        run_dir = bound_manifest.path.parent.resolve()
        self._verify_version_anchor(version, manifest, run_dir)
        record = (manifest.get("artifacts") or {}).get("run_report")
        if not isinstance(record, dict):
            raise WorkspaceServiceError(
                "REVIEW_REQUIRED", "目标 version 尚无可验证的 run_report 人工结论"
            )
        resolved = _resolved_manifest_file(
            run_id=version["run_id"],
            artifact_name="run_report",
            record=record,
            run_dir=run_dir,
        )
        if resolved.path.name != "run_report.json":
            raise ArtifactResolutionError(
                "ARTIFACT_INVALID", "run_report 记录未指向 run_report.json"
            )
        try:
            report = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactResolutionError(
                "ARTIFACT_INVALID", "run_report 不是可读 JSON"
            ) from exc
        if not isinstance(report, dict):
            raise ArtifactResolutionError(
                "ARTIFACT_INVALID", "run_report 必须是 JSON object"
            )
        if str(report.get("run_id") or "") != version["run_id"]:
            raise ArtifactResolutionError(
                "ARTIFACT_ID_MISMATCH", "run_report.run_id 与目标 version 不一致"
            )
        if str(report.get("machine_audit_status") or "").strip() != "pass":
            raise WorkspaceServiceError(
                "REVIEW_REQUIRED", "run_report 机器审核未通过，不能登记版本人工结论"
            )
        decision = str(report.get("human_review_status") or "").strip()
        if decision not in {"quality_pass", "quality_fail"}:
            raise WorkspaceServiceError(
                "REVIEW_REQUIRED",
                "run_report 人工结论仍为 pending、invalid 或未知，不能登记版本审核",
            )
        self.services.artifacts.resolve_manifest(
            version["run_id"], expected_sha256=binding["manifest_sha256"]
        )
        return {
            "decision": decision,
            "evidence_ref": f"artifact://run/{version['run_id']}/run_report",
            "run_report_sha256": resolved.sha256,
        }

    def _require_publishable(
        self,
        auth_token: str,
        *,
        project_id: str,
        version_id: str,
        review_id: str,
    ) -> None:
        governance = self._version_governance(auth_token, version_id)
        latest_review = governance.get("latest_review") or {}
        if (
            latest_review.get("review_id") != review_id
            or latest_review.get("decision") != "quality_pass"
        ):
            raise WorkspaceServiceError(
                "REVIEW_REQUIRED", "发布前必须使用目标 version 最新人工 quality_pass"
            )
        if self._current_candidate_id(project_id) != (
            governance.get("latest_candidate") or {}
        ).get("candidate_id"):
            raise WorkspaceServiceError(
                "CANDIDATE_REQUIRED", "只有当前候选 version 可以发布"
            )
        if governance["publications"]:
            raise WorkspaceServiceError(
                "REVIEW_REQUIRED", "目标 version 已有正式发布登记，不能重复发布"
            )

    def _version_governance(self, auth_token: str, version_id: str) -> dict:
        reviews = self.services.versions.list_reviews(auth_token, version_id)
        candidates = self.services.versions.list_candidates(auth_token, version_id)
        publications = self.services.versions.list_publications(
            auth_token, version_id
        )
        return {
            "reviews": reviews,
            "candidates": candidates,
            "publications": publications,
            "latest_review": reviews[-1] if reviews else None,
            "latest_candidate": candidates[-1] if candidates else None,
            "latest_publication": publications[-1] if publications else None,
        }

    def _current_candidate_id(self, project_id: str) -> str | None:
        with self.services.store.connection() as connection:
            row = connection.execute(
                """
                SELECT candidate_id FROM candidate_selections
                WHERE project_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        return str(row["candidate_id"]) if row is not None else None

    def _version_artifacts(
        self,
        auth_token: str,
        *,
        project_id: str,
        version_id: str,
    ) -> dict:
        version = self._require_project_version(
            auth_token, project_id=project_id, version_id=version_id
        )
        binding = self.services.projects.get_run_binding(
            auth_token, version["run_id"]
        )
        if binding["project_id"] != project_id:
            raise WorkspaceServiceError(
                "PERMISSION_DENIED", "version run 不属于当前 project"
            )
        manifest = self.services.artifacts.read_manifest(
            version["run_id"], expected_sha256=binding["manifest_sha256"]
        )
        bound_manifest = self.services.artifacts.resolve_manifest(
            version["run_id"], expected_sha256=binding["manifest_sha256"]
        )
        run_dir = bound_manifest.path.parent.resolve()
        self._verify_version_anchor(version, manifest, run_dir)

        payloads: dict[str, dict] = {}
        evidence = {
            "version_id": version_id,
            "run_id": version["run_id"],
            "manifest_ref": binding["manifest_ref"],
            "manifest_sha256": binding["manifest_sha256"],
            "version_artifact_ref": version["artifact_ref"],
            "version_content_sha256": version["content_sha256"],
            "artifacts": {},
        }
        for artifact_name in ("script_plan", "script_draft", "visual_binding"):
            resolved = self._resolve_run_artifact(
                version["run_id"], artifact_name, manifest, run_dir
            )
            payloads[artifact_name] = _read_json_artifact(
                resolved, artifact_name=artifact_name
            )
            evidence["artifacts"][artifact_name] = {
                "ref": resolved.ref,
                "sha256": resolved.sha256,
                "size_bytes": resolved.size_bytes,
            }
        _validate_artifact_chain(payloads)
        self.services.artifacts.resolve_manifest(
            version["run_id"], expected_sha256=binding["manifest_sha256"]
        )
        visual = payloads["visual_binding"]
        project = self.services.projects.get_project(auth_token, project_id)
        context = VersionDeliveryContext(
            schema_version="mplus-version-delivery-context/v1",
            workspace_id=project["workspace_id"],
            project_id=project_id,
            project_lock_version=project["project_version"],
            version_id=version_id,
            run_id=version["run_id"],
            parent_version_id=version.get("parent_version_id"),
            version_artifact=VersionDeliveryArtifact(version["artifact_ref"], version["content_sha256"]),
            manifest=VersionDeliveryArtifact(bound_manifest.ref, bound_manifest.sha256),
            artifacts=VersionDeliveryArtifacts(**{
                name: VersionDeliveryArtifact(item["ref"], item["sha256"])
                for name, item in evidence["artifacts"].items()
            }),
        )
        shot_ids = resolve_version_shot_ids(self.services.artifacts, context, visual["script_rows"])
        shots = version_shots_from_artifacts(
            visual["script_rows"],
            visual["detail_rows"],
            bindings=visual["bindings"],
        )
        return {
            "shots": _redact_absolute_paths(shots),
            "shot_ids": shot_ids,
            "has_frozen_lineage": "revision_report" in (manifest.get("artifacts") or {}),
            "evidence": evidence,
        }

    def _require_project_version(
        self,
        auth_token: str,
        *,
        project_id: str,
        version_id: str,
    ) -> dict:
        self.services.projects.get_project(auth_token, project_id)
        version = self.services.projects.get_project_version(
            auth_token, version_id
        )
        if version["project_id"] != project_id:
            raise WorkspaceServiceError(
                "PERMISSION_DENIED", "version 不属于当前 project"
            )
        return version

    def _verify_version_anchor(
        self,
        version: dict,
        manifest: dict,
        run_dir: Path,
    ) -> None:
        run_id = version["run_id"]
        prefix = f"artifact://run/{run_id}/"
        artifact_ref = str(version["artifact_ref"])
        if not artifact_ref.startswith(prefix):
            raise ArtifactResolutionError(
                "ARTIFACT_SCOPE_VIOLATION", "version artifact_ref 与 run 不一致"
            )
        artifact_name = artifact_ref[len(prefix):]
        if artifact_name == "manifest":
            resolved = self.services.artifacts.resolve_manifest(
                run_id, expected_sha256=version["content_sha256"]
            )
        else:
            resolved = self._resolve_run_artifact(
                run_id, artifact_name, manifest, run_dir
            )
            if resolved.sha256 != version["content_sha256"]:
                raise ArtifactResolutionError(
                    "ARTIFACT_HASH_MISMATCH",
                    "version content SHA-256 与 artifact_ref 不一致",
                )

    def _resolve_run_artifact(
        self,
        run_id: str,
        artifact_name: str,
        manifest: dict,
        run_dir: Path,
    ) -> ResolvedArtifact:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", artifact_name):
            raise ArtifactResolutionError(
                "INVALID_INPUT", "artifact_name 无效"
            )
        top_level = (manifest.get("artifacts") or {}).get(artifact_name)
        if isinstance(top_level, dict):
            return _resolved_manifest_file(
                run_id=run_id,
                artifact_name=artifact_name,
                record=top_level,
                run_dir=run_dir,
            )
        record = (manifest.get("stages") or {}).get(artifact_name)
        if not isinstance(record, dict) or record.get("status") not in {
            "succeeded",
            "succeeded_with_warnings",
        }:
            raise ArtifactResolutionError(
                "NOT_FOUND", f"{artifact_name} 未形成可比较的成功阶段产物"
            )
        refs = record.get("output_refs")
        expected_sha256 = str(record.get("output_sha256") or "").lower()
        if (
            not isinstance(refs, list)
            or len(refs) != 1
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        ):
            raise ArtifactResolutionError(
                "ARTIFACT_INVALID", f"{artifact_name} 阶段缺少唯一产物或 SHA-256"
            )
        path = resolve_run_local_path(run_dir, refs[0])
        try:
            path.relative_to(run_dir)
        except ValueError as exc:
            raise ArtifactResolutionError(
                "ARTIFACT_SCOPE_VIOLATION", f"{artifact_name} 阶段产物不在所属 run"
            ) from exc
        if path.name != f"{artifact_name}.json" or not path.is_file():
            raise ArtifactResolutionError(
                "NOT_FOUND", f"{artifact_name} 阶段产物文件不存在"
            )
        digest = _sha256_file(path)
        if digest != expected_sha256:
            raise ArtifactResolutionError(
                "ARTIFACT_HASH_MISMATCH", f"{artifact_name} 阶段产物 SHA-256 已变化"
            )
        return ResolvedArtifact(
            ref=f"artifact://run/{run_id}/{artifact_name}",
            path=path,
            sha256=digest,
            size_bytes=path.stat().st_size,
        )

    def _controlled_write(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        authority: Callable[[], dict],
        action: Callable[[], dict],
        result_ref: Callable[[dict], str],
    ) -> dict:
        idempotency = self._manual_idempotency()
        claim = idempotency.claim(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=parameters,
        )
        if claim["replayed"]:
            if claim["response"] is None:
                raise VersionWorkbenchRecoveryRequired(
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
            expected_project_version=parameters["expected_project_version"],
        )
        task = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="executing",
            details={"tool_name": tool_name},
        )
        self._checkpoint("after_task_started")
        try:
            authority()
            self._manual_confirmations().consume(
                auth_token,
                confirmation_token=parameters["confirmation_token"],
                agent_session_id=agent_session_id,
                tool_name=tool_name,
                parameters=parameters,
            )
            self._checkpoint("after_confirmation_consumed")
            authority()
            business_result = action()
            self._checkpoint("after_domain_write")
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            error = _public_controlled_error(exc)
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

        logical_ref = result_ref(business_result)
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
                "warnings": [],
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

    def _manual_confirmations(self) -> ManualPageConfirmationService:
        service = self.services.confirmations
        if isinstance(service, ManualPageConfirmationService):
            return service
        return ManualPageConfirmationService(service.store, service.signing_key)

    def _manual_idempotency(self) -> ManualPageIdempotencyService:
        service = self.services.idempotency
        if isinstance(service, ManualPageIdempotencyService):
            return service
        return ManualPageIdempotencyService(service.store)

    def _replayed_result(
        self,
        auth_token: str,
        tool_name: str,
        stored: dict,
    ) -> dict:
        if stored["status"] == "completed":
            events = self.services.tasks.list_task_events(
                auth_token, stored["task_id"]
            )
            completed = [item for item in events if item["status"] == "completed"]
            if not completed or "result" not in completed[-1]["details"]:
                raise VersionWorkbenchRecoveryRequired(
                    tool_name=tool_name,
                    idempotency_record_id="recorded-result-missing-task-evidence",
                )
            business_result = completed[-1]["details"]["result"]
        else:
            business_result = None
        return {
            "tool_name": tool_name,
            "task_id": stored["task_id"],
            "status": stored["status"],
            "replayed": True,
            "result": business_result,
            "error": stored.get("error"),
        }


def _read_json_artifact(
    artifact: ResolvedArtifact,
    *,
    artifact_name: str,
) -> dict:
    try:
        payload = json.loads(artifact.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactResolutionError(
            "ARTIFACT_INVALID", f"{artifact_name} 不是可读 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ArtifactResolutionError(
            "ARTIFACT_INVALID", f"{artifact_name} 必须是 JSON object"
        )
    if payload.get("schema_version") not in _EXPECTED_SCHEMAS[artifact_name]:
        raise ArtifactResolutionError(
            "ARTIFACT_INVALID", f"{artifact_name} schema_version 不受支持"
        )
    for field in ("script_rows", "detail_rows"):
        if not isinstance(payload.get(field), list):
            raise ArtifactResolutionError(
                "ARTIFACT_INVALID", f"{artifact_name}.{field} 必须是 array"
            )
    if artifact_name == "visual_binding" and not isinstance(
        payload.get("bindings"), list
    ):
        raise ArtifactResolutionError(
            "ARTIFACT_INVALID", "visual_binding.bindings 必须是 array"
        )
    return payload


def _resolved_manifest_file(
    *,
    run_id: str,
    artifact_name: str,
    record: dict,
    run_dir: Path,
) -> ResolvedArtifact:
    raw_path = str(record.get("path") or "").strip()
    expected_sha256 = str(record.get("sha256") or "").lower()
    if not raw_path or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ArtifactResolutionError(
            "ARTIFACT_INVALID", f"{artifact_name} 记录缺少路径或 SHA-256"
        )
    path = resolve_run_local_path(run_dir, raw_path)
    try:
        path.relative_to(run_dir)
    except ValueError as exc:
        raise ArtifactResolutionError(
            "ARTIFACT_SCOPE_VIOLATION", f"{artifact_name} 不在所属 run"
        ) from exc
    if not path.is_file():
        raise ArtifactResolutionError(
            "NOT_FOUND", f"{artifact_name} 文件不存在"
        )
    digest = _sha256_file(path)
    if digest != expected_sha256:
        raise ArtifactResolutionError(
            "ARTIFACT_HASH_MISMATCH", f"{artifact_name} SHA-256 已变化"
        )
    return ResolvedArtifact(
        ref=f"artifact://run/{run_id}/{artifact_name}",
        path=path,
        sha256=digest,
        size_bytes=path.stat().st_size,
    )


def _validate_artifact_chain(payloads: dict[str, dict]) -> None:
    expected_shot_nos: list[str] | None = None
    for name in ("script_plan", "script_draft", "visual_binding"):
        payload = payloads[name]
        script_rows = payload["script_rows"]
        detail_rows = payload["detail_rows"]
        if len(script_rows) != len(detail_rows):
            raise ArtifactResolutionError(
                "ARTIFACT_INVALID", f"{name} script/detail 数量不一致"
            )
        shot_nos = []
        for row in script_rows:
            if not isinstance(row, dict):
                raise ArtifactResolutionError(
                    "ARTIFACT_INVALID", f"{name}.script_rows 含非 object"
                )
            shot_no = str(row.get("镜号") or row.get("shot_no") or "").strip()
            if not shot_no or shot_no in shot_nos:
                raise ArtifactResolutionError(
                    "ARTIFACT_INVALID", f"{name} 镜号缺失或重复"
                )
            shot_nos.append(shot_no)
        if expected_shot_nos is None:
            expected_shot_nos = shot_nos
        elif shot_nos != expected_shot_nos:
            raise ArtifactResolutionError(
                "ARTIFACT_INVALID", "script_plan/draft/visual 镜头序列不一致"
            )


def _public_controlled_error(
    exc: WorkspaceServiceError | ArtifactResolutionError,
) -> dict:
    if isinstance(exc, ArtifactResolutionError):
        return {
            "code": "REVIEW_REQUIRED",
            "message": f"人工审核证据完整性校验未通过：{exc.message}",
            "retryable": False,
            "details": {"source_code": exc.code},
        }
    return _public_tool_error(exc)


def _public_tool_error(exc: WorkspaceServiceError) -> dict:
    source_code = str(exc.code)
    if source_code in {
        "INVALID_INPUT",
        "NOT_FOUND",
        "REVIEW_REQUIRED",
        "PERMISSION_DENIED",
        "WORKSPACE_VERSION_CONFLICT",
        "PROJECT_VERSION_CONFLICT",
        "IDEMPOTENCY_CONFLICT",
    }:
        code = source_code
    elif source_code == "CANDIDATE_REQUIRED":
        code = "REVIEW_REQUIRED"
    elif source_code in {"AUTH_SESSION_INVALID", "INVALID_PARENT_VERSION"}:
        code = "PERMISSION_DENIED"
    else:
        code = "INTERNAL_ERROR"
    return {
        "code": code,
        "message": str(exc.message),
        "retryable": code in {
            "WORKSPACE_VERSION_CONFLICT",
            "PROJECT_VERSION_CONFLICT",
        },
        "details": {"source_code": source_code},
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact_absolute_paths(value):
    if isinstance(value, dict):
        return {key: _redact_absolute_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_absolute_paths(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_absolute_paths(item) for item in value]
    if isinstance(value, str) and (
        Path(value).is_absolute() or _WINDOWS_ABSOLUTE_PATH.match(value)
    ):
        normalized = value.replace("\\", "/").rstrip("/")
        basename = normalized.rsplit("/", 1)[-1]
        return basename or "[absolute-path-redacted]"
    return value
