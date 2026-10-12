from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from .creative_strategy_tools import CreativeStrategyToolService
from .identity_service import LocalIdentityAdapter, require_permission
from .knowledge_tools import KnowledgeToolService
from .manual_knowledge_commands import ManualKnowledgeApprovalCommandService
from .workspace_store import WorkspaceServiceError


_SCOPES = {"company", "category", "project", "personal"}


@dataclass(frozen=True)
class UiGovernanceServices:
    strategies: CreativeStrategyToolService
    knowledge: KnowledgeToolService
    identity: LocalIdentityAdapter
    manual_knowledge_approval: ManualKnowledgeApprovalCommandService | None = None
    version_reader: Any = None


class UiGovernanceService:
    """Authenticated UI adapter for E06 strategy choice and E07 governance."""

    def __init__(self, services: UiGovernanceServices):
        self.services = services

    def prepare_manual_knowledge_approval(
        self, auth_token: str, *, project_id: str, parameters: dict[str, Any]
    ) -> dict:
        if self.services.manual_knowledge_approval is None:
            raise WorkspaceServiceError("NOT_FOUND", "人工知识批准命令服务未配置")
        return self.services.manual_knowledge_approval.prepare(
            auth_token, project_id=project_id, parameters=parameters
        )

    def execute_manual_knowledge_approval(
        self, auth_token: str, *, project_id: str, parameters: dict[str, Any], command_token: str
    ) -> dict:
        if self.services.manual_knowledge_approval is None:
            raise WorkspaceServiceError("NOT_FOUND", "人工知识批准命令服务未配置")
        return self.services.manual_knowledge_approval.execute(
            auth_token, project_id=project_id, parameters=parameters, command_token=command_token
        )

    def evidence_options(self, auth_token: str, *, agent_session_id: str) -> dict:
        context = self.services.knowledge._session(auth_token, agent_session_id=agent_session_id, permission="project.read")
        options = self._project_evidence(auth_token, context["project_id"], context["workspace_id"])
        return {"project_id": context["project_id"], "items": [
            {key: value for key, value in item.items() if key != "artifact_ref"} for item in options
        ]}

    def _project_evidence(self, auth_token: str, project_id: str, workspace_id: str) -> list[dict]:
        """Read registered identity links, then verify through existing artifact authorities."""
        from pathlib import Path
        from .formal_brief_input_adapter import _FormalInputStore
        from .formal_brief_domain import ArtifactPayload
        from .formal_brief_workspace_service import FormalBriefArtifactStore
        from .project_image_authority import ProjectImageArtifactStore, ArtifactEvidence
        data_root = self.services.strategies.services.briefs.root.parent.parent
        inputs = _FormalInputStore(data_root)
        images = ProjectImageArtifactStore(data_root)
        with self.services.knowledge.services.store.connection() as connection:
            require_permission(connection, auth_token, workspace_id=workspace_id, project_id=project_id, permission="project.read")
            brief_rows = connection.execute("SELECT * FROM project_brief_snapshots WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
            files = connection.execute("SELECT * FROM project_files WHERE project_id=? AND workspace_id=? AND status IN ('frozen','extracted') ORDER BY created_at DESC", (project_id, workspace_id)).fetchall()
            analyses = connection.execute("SELECT * FROM project_image_analysis_versions WHERE project_id=? AND workspace_id=? ORDER BY created_at DESC", (project_id, workspace_id)).fetchall()
            versions = connection.execute("SELECT * FROM project_versions WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
        result = []
        for kind, rows in (("brief", brief_rows), ("file", files), ("analysis", analyses), ("version", versions)):
            for row in rows:
                item = dict(row)
                ref = item.get("brief_ref") or item["artifact_ref"]
                label = {"brief": "已冻结需求", "file": "导入资料", "analysis": "素材分析证据", "version": "已保存版本"}[kind]
                available = True
                try:
                    if kind == "brief":
                        if ref.startswith("artifact://formal-brief/"):
                            FormalBriefArtifactStore(data_root).read(ArtifactPayload(ref, item["brief_sha256"]))
                        else:
                            self.services.strategies.services.briefs.read(ref, item["brief_sha256"])
                        identity = item["brief_snapshot_id"]
                    elif kind == "file":
                        identity = item["file_id"]
                        label = item["filename"]
                        inputs.read(ArtifactPayload(ref, item["sha256"]), suffix=Path(item["filename"]).suffix.lower())
                    elif kind == "analysis":
                        identity = item["project_image_analysis_version_id"]
                        images.read_analysis(ArtifactEvidence(ref, item["artifact_sha256"]))
                    else:
                        identity = item["version_id"]
                        if self.services.version_reader is None:
                            raise WorkspaceServiceError("NOT_FOUND", "版本证据读取尚未配置")
                        self.services.version_reader.read_version(auth_token, project_id=project_id, version_id=identity)
                except (OSError, ValueError, RuntimeError) as exc:
                    available = False
                    identity = item.get("brief_snapshot_id") or item.get("file_id") or item.get("project_image_analysis_version_id") or item.get("version_id")
                result.append({"evidence_id": f"{kind}:{identity}", "artifact_ref": ref,
                    "label": label, "kind": kind, "created_at": item["created_at"], "available": available,
                    "unavailable_reason": "" if available else "原资料已不可读取或校验未通过，请重新核对来源。"})
        return result

    def _validated_evidence(self, auth_token: str, *, agent_session_id: str,
                            evidence_refs: list[str], evidence_ids: list[str] | None = None) -> list[str]:
        context = self.services.knowledge._session(auth_token, agent_session_id=agent_session_id, permission="knowledge.propose")
        options = self._project_evidence(auth_token, context["project_id"], context["workspace_id"])
        by_id = {item["evidence_id"]: item["artifact_ref"] for item in options if item["available"]}
        allowed = set(by_id.values())
        if evidence_ids:
            if evidence_refs or any(value not in by_id for value in evidence_ids):
                raise WorkspaceServiceError("EVIDENCE_STALE", "所选证据不属于当前项目或已经失效，请重新读取并选择")
            selected = [by_id[value] for value in evidence_ids]
        else:
            selected = list(evidence_refs)
        if not selected or any(value not in allowed for value in selected):
            raise WorkspaceServiceError("EVIDENCE_STALE", "证据未通过当前项目来源校验，请重新读取并选择")
        return list(dict.fromkeys(selected))

    def strategy_context(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
    ) -> dict:
        return self.services.strategies.read_creative_strategies(
            auth_token, agent_session_id=agent_session_id,
        )

    def analyze_strategy_context(
        self, auth_token: str, *, agent_session_id: str,
    ) -> dict:
        session = self.services.strategies._session(
            auth_token,
            agent_session_id=agent_session_id,
            permission="project.diagnose",
        )
        result = self.services.strategies.get_creative_strategies(
            auth_token,
            agent_session_id=agent_session_id,
            parameters={"project_id": session["project_id"]},
        )
        if result["status"] != "completed" or not result.get("result"):
            return {
                "project_id": session["project_id"],
                "project_version": session["project_version"],
                "status": "analyzing" if result["status"] in {"received", "executing"} else "blocked",
                "task_id": result.get("task_id"),
                "provider_calls": 0,
                "warnings": list(result.get("warnings") or []),
                "error": result.get("error"),
                "portfolio": None,
            }
        # Inputs may change while analysis runs. Revalidate before projecting
        # an available result, while retaining the immutable original task.
        current = self.strategy_context(auth_token, agent_session_id=agent_session_id)
        return {**current, "task_id": result.get("task_id"),
                "replayed": result.get("replayed", False)}

    def prepare_strategy_selection(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        strategy_portfolio_ref: str,
        strategy_id: str,
        idempotency_key: str,
    ) -> dict:
        current = self.services.strategies._current_project_input(
            auth_token,
            agent_session_id=agent_session_id,
            permission="run.generate",
        )
        artifact = self.services.strategies.services.portfolios.read(
            strategy_portfolio_ref
        )
        parameters = {
            "project_id": current["project_id"],
            "expected_project_version": current["project_version"],
            "idempotency_key": idempotency_key,
            "strategy_portfolio_ref": strategy_portfolio_ref,
            "plan_sha256": artifact["portfolio"]["plan_sha256"],
            "strategy_id": strategy_id,
        }
        confirmation = self.services.strategies.issue_selection_confirmation(
            auth_token,
            agent_session_id=agent_session_id,
            parameters=parameters,
        )
        return {
            **confirmation,
            "risk": "R2",
            "parameters": parameters,
            "calls_models": False,
            "provider_calls": 0,
            "will_change": "创建派生 Brief，并把它设为当前项目快照",
            "will_not_change": "不会生成脚本、不会改变已确认事实与事件集合、不会发布版本或更新 CURRENT",
            "impact_scope": f"仅当前项目 {current['project_id']}",
        }

    def execute_strategy_selection(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
        confirmation_token: str,
    ) -> dict:
        return self.services.strategies.select_creative_strategy(
            auth_token,
            agent_session_id=agent_session_id,
            parameters=parameters,
            confirmation_token=confirmation_token,
        )

    def knowledge_context(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
    ) -> dict:
        context = self.services.knowledge._session(
            auth_token,
            agent_session_id=agent_session_id,
            permission="project.read",
        )
        effective = self.services.knowledge.services.knowledge.list_effective_rules(
            auth_token, context["project_id"]
        )
        targets = {
            "company": context["workspace_id"],
            "category": context["category_id"],
            "project": context["project_id"],
            "personal": context["actor_id"],
        }
        visible_candidates = self._visible_candidates(
            workspace_id=context["workspace_id"], targets=targets
        )
        identity = self.services.identity.resolve_auth_session(auth_token)
        return {
            "workspace_id": context["workspace_id"],
            "project_id": context["project_id"],
            "category_id": context["category_id"],
            "actor_id": context["actor_id"],
            "actor_display_name": identity["display_name"],
            "knowledge_version": effective["knowledge_version"],
            "targets": targets,
            "precedence": list(effective["precedence"]),
            "effective_rules": list(effective["rules"]),
            "pending_candidates": [
                item
                for item in visible_candidates
                if item["status"] == "pending_approval"
            ],
            "pending_candidates_included": False,
            "can_approve": self._can_approve(
                auth_token,
                workspace_id=context["workspace_id"],
                project_id=context["project_id"],
            ),
            "provider_calls": 0,
        }

    def prepare_knowledge_candidate(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        scope: str,
        statement: str,
        evidence_refs: list[str],
        idempotency_key: str,
        evidence_ids: list[str] | None = None,
    ) -> dict:
        if scope not in _SCOPES:
            raise WorkspaceServiceError("INVALID_INPUT", "知识范围不受支持")
        context = self.knowledge_context(
            auth_token, agent_session_id=agent_session_id
        )
        evidence_refs = self._validated_evidence(auth_token, agent_session_id=agent_session_id, evidence_refs=evidence_refs, evidence_ids=evidence_ids)
        parameters = {
            "idempotency_key": idempotency_key,
            "expected_knowledge_version": context["knowledge_version"],
            "scope": scope,
            "target_id": context["targets"][scope],
            "statement": statement,
            "evidence_refs": evidence_refs,
        }
        confirmation = self.services.knowledge.issue_confirmation(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name="create_knowledge_candidate",
            parameters=parameters,
        )
        return {
            **confirmation,
            "risk": "R2",
            "parameters": parameters,
            "calls_models": False,
            "provider_calls": 0,
            "will_change": f"新增一条 {scope} 范围的待审批知识候选",
            "will_not_change": "不会进入有效规则、不会推广到其他层级、不会修改公司规则或发布状态",
            "impact_scope": f"{scope} / {context['targets'][scope]}",
        }

    def execute_knowledge_candidate(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
        confirmation_token: str,
    ) -> dict:
        self._validated_evidence(auth_token, agent_session_id=agent_session_id, evidence_refs=parameters.get("evidence_refs", []))
        return self.services.knowledge.create_knowledge_candidate(
            auth_token,
            agent_session_id=agent_session_id,
            parameters=parameters,
            confirmation_token=confirmation_token,
        )

    def prepare_knowledge_approval(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        candidate_id: str,
        review_evidence: str,
        idempotency_key: str,
    ) -> dict:
        context = self.knowledge_context(
            auth_token, agent_session_id=agent_session_id
        )
        if not context["can_approve"]:
            raise WorkspaceServiceError("PERMISSION_DENIED", "仅管理员可批准知识候选")
        candidate = next(
            (
                item
                for item in context["pending_candidates"]
                if item["candidate_id"] == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise WorkspaceServiceError(
                "NOT_FOUND", "当前项目范围内没有这条待审批候选"
            )
        parameters = {
            "idempotency_key": idempotency_key,
            "expected_knowledge_version": context["knowledge_version"],
            "candidate_id": candidate_id,
            "level": candidate["scope"],
            "review_evidence": review_evidence,
        }
        confirmation = self.services.knowledge.issue_confirmation(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name="approve_knowledge",
            parameters=parameters,
        )
        return {
            **confirmation,
            "risk": "R3",
            "parameters": parameters,
            "calls_models": False,
            "provider_calls": 0,
            "will_change": f"把该候选激活为 {candidate['scope']} 范围的有效规则",
            "will_not_change": "不会跨层级推广、不会改写候选原文、不会发布脚本版本或更新 CURRENT",
            "impact_scope": f"{candidate['scope']} / {candidate['target_id']}",
        }

    def execute_knowledge_approval(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
        confirmation_token: str,
    ) -> dict:
        return self.services.knowledge.approve_knowledge(
            auth_token,
            agent_session_id=agent_session_id,
            parameters={
                **parameters,
                "confirmation_token": confirmation_token,
            },
        )

    def _can_approve(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
    ) -> bool:
        try:
            with self.services.knowledge.services.store.connection() as connection:
                require_permission(
                    connection,
                    auth_token,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    permission="knowledge.approve",
                )
        except WorkspaceServiceError:
            return False
        return True

    def _visible_candidates(
        self,
        *,
        workspace_id: str,
        targets: dict[str, str],
    ) -> list[dict]:
        with self.services.knowledge.services.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT c.*, a.approval_id, r.rule_id, r.active_version
                FROM knowledge_candidates c
                LEFT JOIN knowledge_approvals a ON a.candidate_id = c.candidate_id
                LEFT JOIN knowledge_rules r ON r.candidate_id = c.candidate_id
                WHERE c.workspace_id = ? AND (
                    (c.scope = 'company' AND c.target_id = ?) OR
                    (c.scope = 'category' AND c.target_id = ?) OR
                    (c.scope = 'project' AND c.target_id = ?) OR
                    (c.scope = 'personal' AND c.target_id = ?)
                )
                ORDER BY c.created_at, c.candidate_id
                """,
                (
                    workspace_id,
                    targets["company"],
                    targets["category"],
                    targets["project"],
                    targets["personal"],
                ),
            ).fetchall()
        return [
            {
                "candidate_id": row["candidate_id"],
                "workspace_id": row["workspace_id"],
                "scope": row["scope"],
                "target_id": row["target_id"],
                "statement": row["statement"],
                "statement_sha256": row["statement_sha256"],
                "evidence_refs": json.loads(row["evidence_refs_json"]),
                "proposed_by_actor_id": row["proposed_by_actor_id"],
                "knowledge_version": row["knowledge_version"],
                "status": "active" if row["approval_id"] else "pending_approval",
                "approval_id": row["approval_id"],
                "rule_id": row["rule_id"],
                "active_version": row["active_version"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
