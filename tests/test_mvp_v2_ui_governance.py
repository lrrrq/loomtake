from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.ai_spec import refresh_ai_spec
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.asset_package import create_asset_package
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.creative_strategy_artifacts import CreativeStrategyArtifactStore
from mvp_v2.creative_strategy_tools import (
    CreativeStrategyPackageResolver,
    CreativeStrategyToolService,
    CreativeStrategyToolServices,
)
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.knowledge_service import KnowledgeService
from mvp_v2.knowledge_tools import KnowledgeToolService, KnowledgeToolServices
from mvp_v2.manual_knowledge_commands import ManualKnowledgeApprovalCommandService, ManualKnowledgeApprovalCommandServices
from mvp_v2.local_ui import create_app
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.ui_governance import UiGovernanceService, UiGovernanceServices
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceStore


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "creative_strategy"
    / "six_category_briefs.json"
)


class GovernanceUiFixture:
    def __init__(self, root: Path, *, complete_brief: bool = True):
        self.root = root
        self.data_root = root / "runtime"
        source = root / "anonymous-source"
        source.mkdir(parents=True)
        workbook_path = source / "M+发布会类通用脚本库.xlsx"
        _write_source_workbook(workbook_path)
        report = refresh_ai_spec(workbook_path)
        if not report.in_sync or not report.usable:
            raise AssertionError("匿名 AI_规范表 fixture 未通过质量门")
        manifest = create_asset_package(
            root / "package",
            package_id="anonymous-governance-launch",
            source_root=source,
            category_id="launch",
        )
        self.runtime = {
            "data_root": str(self.data_root),
            "workspace_db": str(self.data_root / "workspace.sqlite3"),
            "asset_packages": {"发布会": str(manifest)},
            "asset_package": "",
        }
        self.store = WorkspaceStore(self.data_root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="匿名管理员",
            confirmed=True,
            token="anonymous-governance-owner-token",
        )
        self.auth = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.tasks = TaskService(self.store)
        self.briefs = BriefArtifactStore(self.data_root)
        source_brief = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][0]["brief"]
        self.brief = deepcopy(source_brief)
        if not complete_brief:
            self.brief.pop("叙事重点", None)
            self.brief.pop("重点前置理由", None)
            self.brief.pop("情绪递进理由", None)
            self.brief.pop("planning_events", None)
        frozen = self.briefs.freeze(self.brief)
        self.project = self.projects.create_project(
            self.auth,
            workspace_id=self.workspace_id,
            name="匿名治理项目",
            category_id="launch",
            expected_workspace_version=0,
            brief_ref=frozen["brief_ref"],
            brief_sha256=frozen["brief_sha256"],
        )
        self.session = self.tasks.create_agent_session(
            self.auth, project_id=self.project["project_id"]
        )["agent_session_id"]
        workspace = self.projects.get_workspace(self.auth, self.workspace_id)
        editor = self.identity.create_actor(
            self.auth,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            display_name="匿名编辑",
            auth_source="local",
            role="editor",
            expected_workspace_version=workspace["workspace_version"],
        )
        self.editor_auth = self.identity.issue_auth_session(
            actor_id=editor["actor_id"],
            token="anonymous-governance-editor-token",
        )["auth_token"]
        self.editor_session = self.tasks.create_agent_session(
            self.editor_auth, project_id=self.project["project_id"]
        )["agent_session_id"]
        confirmations = ActionConfirmationService(self.store, b"g" * 32)
        strategy = CreativeStrategyToolService(
            CreativeStrategyToolServices(
                store=self.store,
                projects=self.projects,
                tasks=self.tasks,
                confirmations=confirmations,
                idempotency=ToolIdempotencyService(self.store),
                briefs=self.briefs,
                portfolios=CreativeStrategyArtifactStore(self.data_root),
                packages=CreativeStrategyPackageResolver(self.runtime),
            )
        )
        knowledge = KnowledgeToolService(
            KnowledgeToolServices(
                store=self.store,
                projects=self.projects,
                knowledge=KnowledgeService(self.store),
                tasks=self.tasks,
                confirmations=confirmations,
                idempotency=ToolIdempotencyService(self.store),
            )
        )
        governance = UiGovernanceService(
            UiGovernanceServices(
                strategies=strategy,
                knowledge=knowledge,
                identity=self.identity,
                manual_knowledge_approval=ManualKnowledgeApprovalCommandService(
                    ManualKnowledgeApprovalCommandServices(
                        store=self.store,
                        projects=self.projects,
                        knowledge=KnowledgeService(self.store),
                    )
                ),
            )
        )
        agent_api = ReadonlyAgentService(
            ReadonlyAgentServices(
                store=self.store,
                identity=self.identity,
                projects=self.projects,
                tasks=self.tasks,
                versions=VersionService(self.store),
                artifacts=RunArtifactResolver(root / "runs"),
            )
        )
        self.app = create_app(agent_api=agent_api, ui_governance=governance)
        self.client = TestClient(self.app)
        self.headers = {
            "X-MPlus-UI-Token": self.app.state.session_token,
            "X-MPlus-Agent-Auth": self.auth,
        }
        self.manual_headers = {
            "X-MPlus-UI-Token": self.app.state.session_token,
            "X-MPlus-Manual-Auth": self.auth,
        }

    def post(self, path: str, payload: dict, *, headers: dict | None = None):
        return self.client.post(path, json=payload, headers=headers or self.headers)


class GovernanceUiApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = GovernanceUiFixture(Path(self.temporary.name))

    def tearDown(self):
        self.fixture.client.close()
        self.temporary.cleanup()

    def test_strategy_journey_is_csrf_authenticated_confirmed_and_zero_provider(self):
        path = f"/api/agent/governance/strategies?agent_session_id={self.fixture.session}"
        self.assertEqual(self.fixture.client.get(path).status_code, 403)
        self.assertEqual(
            self.fixture.client.get(
                path,
                headers={"X-MPlus-UI-Token": self.fixture.app.state.session_token},
            ).status_code,
            401,
        )
        before = self.fixture.projects.get_current_brief_snapshot(
            self.fixture.auth, self.fixture.project["project_id"]
        )
        with patch("mvp_v2.text_providers.build_text_provider") as provider:
            readonly = self.fixture.client.get(path, headers=self.fixture.headers)
            self.assertEqual(readonly.json()['status'], 'not_analyzed')
            analysis_path = '/api/agent/governance/strategies/analyze'
            self.assertEqual(self.fixture.client.post(analysis_path, json={'agent_session_id': self.fixture.session}).status_code, 403)
            context = self.fixture.post(analysis_path, {'agent_session_id': self.fixture.session})
            self.assertEqual(context.status_code, 200, context.text)
            body = context.json()
            self.assertEqual(body["status"], "available")
            self.assertEqual(body["portfolio"]["status"], "pass")
            self.assertEqual(body["portfolio"]["coverage"]["selectable_direction_count"], 3)
            self.assertEqual(body["provider_calls"], 0)

            prepared = self.fixture.post(
                "/api/agent/governance/strategies/prepare",
                {
                    "agent_session_id": self.fixture.session,
                    "strategy_portfolio_ref": body["portfolio_ref"],
                    "strategy_id": "focus_first",
                    "idempotency_key": "ui-governance-strategy-0001",
                },
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            self.assertEqual(prepared.json()["risk"], "R2")
            still_before = self.fixture.projects.get_current_brief_snapshot(
                self.fixture.auth, self.fixture.project["project_id"]
            )
            self.assertEqual(still_before["brief_snapshot_id"], before["brief_snapshot_id"])

            executed = self.fixture.post(
                "/api/agent/governance/strategies/execute",
                {
                    "agent_session_id": self.fixture.session,
                    "parameters": prepared.json()["parameters"],
                    "confirmation_token": prepared.json()["confirmation_token"],
                },
            )
        provider.assert_not_called()
        self.assertEqual(executed.status_code, 200, executed.text)
        self.assertEqual(executed.json()["status"], "completed")
        self.assertEqual(executed.json()["result"]["provider_calls"], 0)
        after = self.fixture.projects.get_current_brief_snapshot(
            self.fixture.auth, self.fixture.project["project_id"]
        )
        self.assertNotEqual(after["brief_snapshot_id"], before["brief_snapshot_id"])
        derived = self.fixture.briefs.read(after["brief_ref"], after["brief_sha256"])
        self.assertEqual(
            derived["creative_strategy_selection"]["binding"]["strategy_id"],
            "focus_first",
        )
        self.assertNotIn(
            self.temporary.name,
            json.dumps(executed.json(), ensure_ascii=False),
        )

    def test_incomplete_brief_reports_needs_clarification_and_cannot_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = GovernanceUiFixture(Path(tmp), complete_brief=False)
            path = f"/api/agent/governance/strategies?agent_session_id={fixture.session}"
            response = fixture.post("/api/agent/governance/strategies/analyze", {"agent_session_id": fixture.session})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["status"], "available")
            self.assertEqual(body["portfolio"]["status"], "needs_clarification")
            self.assertLess(
                body["portfolio"]["coverage"]["selectable_direction_count"], 2
            )
            prepared = fixture.post(
                "/api/agent/governance/strategies/prepare",
                {
                    "agent_session_id": fixture.session,
                    "strategy_portfolio_ref": body["portfolio_ref"],
                    "strategy_id": "linear_fact_order",
                    "idempotency_key": "ui-governance-strategy-blocked",
                },
            )
            self.assertEqual(prepared.status_code, 409)
            fixture.client.close()

    def test_pending_candidate_is_not_effective_then_admin_r3_approval_activates_it(self):
        context_path = f"/api/agent/governance/knowledge?agent_session_id={self.fixture.session}"
        initial = self.fixture.client.get(context_path, headers=self.fixture.headers)
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertTrue(initial.json()["can_approve"])
        self.assertFalse(initial.json()["pending_candidates_included"])
        self.assertEqual(initial.json()["effective_rules"], [])

        with patch("mvp_v2.text_providers.build_text_provider") as provider:
            prepared = self.fixture.post(
                "/api/agent/governance/knowledge/candidates/prepare",
                {
                    "agent_session_id": self.fixture.session,
                    "scope": "project",
                    "statement": "匿名项目需先展示核心事实再进入情绪推进",
                    "evidence_refs": [self.fixture.projects.get_current_brief_snapshot(self.fixture.auth, self.fixture.project["project_id"])["brief_ref"]],
                    "idempotency_key": "ui-governance-knowledge-create",
                },
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            self.assertEqual(prepared.json()["risk"], "R2")
            self.assertEqual(
                self.fixture.client.get(context_path, headers=self.fixture.headers).json()[
                    "pending_candidates"
                ],
                [],
            )
            created = self.fixture.post(
                "/api/agent/governance/knowledge/candidates/execute",
                {
                    "agent_session_id": self.fixture.session,
                    "parameters": prepared.json()["parameters"],
                    "confirmation_token": prepared.json()["confirmation_token"],
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            pending = created.json()["result"]
            self.assertEqual(pending["status"], "pending_approval")
            middle = self.fixture.client.get(context_path, headers=self.fixture.headers).json()
            self.assertEqual(middle["effective_rules"], [])
            self.assertFalse(middle["pending_candidates_included"])
            self.assertEqual(len(middle["pending_candidates"]), 1)

            blocked_agent = self.fixture.post(
                "/api/agent/governance/knowledge/approvals/prepare",
                {
                    "agent_session_id": self.fixture.session,
                    "candidate_id": pending["candidate_id"],
                    "review_evidence": "管理员已核对 artifact 证据与候选原文",
                    "idempotency_key": "ui-governance-knowledge-approve",
                },
            )
            self.assertEqual(blocked_agent.status_code, 404)
            with self.fixture.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM manual_command_audits").fetchone()[0], 0)
            approval = self.fixture.post(
                "/api/manual/knowledge/approvals/prepare",
                {
                    "project_id": self.fixture.project["project_id"],
                    "candidate_id": pending["candidate_id"],
                    "review_evidence": "管理员已核对 artifact 证据与候选原文",
                    "idempotency_key": "ui-governance-knowledge-approve",
                },
                headers=self.fixture.manual_headers,
            )
            self.assertEqual(approval.status_code, 200, approval.text)
            self.assertEqual(approval.json()["risk"], "R3")
            self.assertEqual(
                approval.json()["parameters"]["level"], pending["scope"]
            )
            approved = self.fixture.post(
                "/api/manual/knowledge/approvals/execute",
                {
                    "project_id": self.fixture.project["project_id"],
                    "parameters": approval.json()["parameters"],
                    "command_token": approval.json()["command_token"],
                },
                headers=self.fixture.manual_headers,
            )
        provider.assert_not_called()
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["result"]["status"], "active")
        replay = self.fixture.post('/api/manual/knowledge/approvals/execute', {
            'project_id': self.fixture.project['project_id'],
            'parameters': approval.json()['parameters'], 'command_token': approval.json()['command_token'],
        }, headers=self.fixture.manual_headers)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json(), approved.json())
        final = self.fixture.client.get(context_path, headers=self.fixture.headers).json()
        self.assertEqual(len(final["effective_rules"]), 1)
        self.assertEqual(final["pending_candidates"], [])

    def test_editor_can_propose_but_context_never_claims_approval_permission(self):
        path = f"/api/agent/governance/knowledge?agent_session_id={self.fixture.editor_session}"
        editor_headers = {
            "X-MPlus-UI-Token": self.fixture.app.state.session_token,
            "X-MPlus-Agent-Auth": self.fixture.editor_auth,
        }
        context = self.fixture.client.get(path, headers=editor_headers)
        self.assertEqual(context.status_code, 200, context.text)
        self.assertFalse(context.json()["can_approve"])
        prepared = self.fixture.post(
            "/api/agent/governance/knowledge/candidates/prepare",
            {
                "agent_session_id": self.fixture.editor_session,
                "scope": "project",
                "statement": "匿名编辑提交的项目候选",
                "evidence_refs": [self.fixture.projects.get_current_brief_snapshot(self.fixture.auth, self.fixture.project["project_id"])["brief_ref"]],
                "idempotency_key": "ui-governance-editor-create",
            },
            headers=editor_headers,
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        approval = self.fixture.post(
            "/api/agent/governance/knowledge/approvals/prepare",
            {
                "agent_session_id": self.fixture.editor_session,
                "candidate_id": "knowledge_candidate_anonymous0001",
                "review_evidence": "不应被接受",
                "idempotency_key": "ui-governance-editor-approve",
            },
            headers=editor_headers,
        )
        self.assertEqual(approval.status_code, 404)


def _write_source_workbook(path: Path) -> None:
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    headers = [
        "结构", "镜号", "形式", "效果", "景别", "运镜", "画面描述", "声音",
        "备注", "镜头功能", "画面主体", "可复用等级", "禁用说明",
    ]
    for column, value in enumerate(headers, start=1):
        template.cell(6, column, value)
    for index in range(1, 4):
        values = [
            "建立" if index == 1 else "推进" if index == 2 else "收束",
            str(index), "实拍", "", "全景" if index == 1 else "中景", "推进",
            f"匿名功能镜头 {index}", "现场声", "匿名 fixture",
            f"匿名镜头功能 {index}", f"匿名事件主体 {index}", "镜头功能可复用", "",
        ]
        for column, value in enumerate(values, start=1):
            template.cell(index + 6, column, value)
    creative = workbook.create_sheet("创意库")
    for column, value in enumerate(headers, start=1):
        creative.cell(6, column, value)
    for column, value in enumerate(
        [
            "推进", "C001", "实拍", "", "中景", "跟随", "匿名创意镜头",
            "现场声", "匿名 fixture", "匿名创意功能", "匿名事件主体",
            "镜头功能可复用", "",
        ],
        start=1,
    ):
        creative.cell(7, column, value)
    workbook.save(path)
    workbook.close()


if __name__ == "__main__":
    unittest.main()
