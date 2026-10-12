from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.ai_spec import refresh_ai_spec
from mvp_v2.asset_package import create_asset_package, package_script_catalog
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.creative_strategy_artifacts import CreativeStrategyArtifactStore
from mvp_v2.creative_strategy_tools import (
    CreativeStrategyPackageResolver,
    CreativeStrategyRecoveryRequired,
    CreativeStrategyToolService,
    CreativeStrategyToolServices,
)
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.planning_core import compile_plan_artifact
from mvp_v2.project_service import ProjectService
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "creative_strategy"
    / "six_category_briefs.json"
)


class StrategyToolFixture:
    def __init__(self, root: Path, *, checkpoint=None):
        self.root = root
        self.data_root = root / "runtime"
        source = root / "anonymous-source"
        source.mkdir(parents=True)
        workbook = source / "M+发布会类通用脚本库.xlsx"
        _write_source_workbook(workbook)
        report = refresh_ai_spec(workbook)
        if not report.in_sync or not report.usable:
            raise AssertionError("匿名 AI_规范表 fixture 未通过质量门")
        self.manifest = create_asset_package(
            root / "package",
            package_id="anonymous-launch-ai-spec",
            source_root=source,
            category_id="launch",
        )
        self.runtime = {
            "data_root": str(self.data_root),
            "workspace_db": str(self.data_root / "workspace.sqlite3"),
            "asset_packages": {"发布会": str(self.manifest)},
            "asset_package": "",
        }
        self.store = WorkspaceStore(self.data_root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="匿名管理员",
            confirmed=True,
            token="anonymous-strategy-owner-token",
        )
        self.token = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.tasks = TaskService(self.store)
        self.briefs = BriefArtifactStore(self.data_root)
        self.portfolios = CreativeStrategyArtifactStore(self.data_root)
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        self.original_brief = deepcopy(cases[0]["brief"])
        frozen = self.briefs.freeze(self.original_brief)
        self.project = self.projects.create_project(
            self.token,
            workspace_id=self.workspace_id,
            name="匿名发布项目",
            category_id="launch",
            expected_workspace_version=0,
            brief_ref=frozen["brief_ref"],
            brief_sha256=frozen["brief_sha256"],
        )
        self.other_project = self.projects.create_project(
            self.token,
            workspace_id=self.workspace_id,
            name="匿名隔离项目",
            category_id="launch",
            expected_workspace_version=1,
            brief_ref=frozen["brief_ref"],
            brief_sha256=frozen["brief_sha256"],
        )
        self.session = self.tasks.create_agent_session(
            self.token, project_id=self.project["project_id"]
        )["agent_session_id"]
        self.other_session = self.tasks.create_agent_session(
            self.token, project_id=self.other_project["project_id"]
        )["agent_session_id"]
        self.confirmations = ActionConfirmationService(self.store, b"s" * 32)
        self.services = CreativeStrategyToolServices(
            store=self.store,
            projects=self.projects,
            tasks=self.tasks,
            confirmations=self.confirmations,
            idempotency=ToolIdempotencyService(self.store),
            briefs=self.briefs,
            portfolios=self.portfolios,
            packages=CreativeStrategyPackageResolver(self.runtime),
        )
        self.tools = CreativeStrategyToolService(
            self.services, checkpoint=checkpoint
        )

    def get(self) -> dict:
        return self.tools.get_creative_strategies(
            self.token,
            agent_session_id=self.session,
            parameters={"project_id": self.project["project_id"]},
        )

    def selection_parameters(self, portfolio: dict, *, key="strategy-select-0001") -> dict:
        return {
            "project_id": self.project["project_id"],
            "expected_project_version": portfolio["result"]["project_version"],
            "idempotency_key": key,
            "strategy_portfolio_ref": portfolio["result"]["portfolio_ref"],
            "plan_sha256": portfolio["result"]["plan_sha256"],
            "strategy_id": "focus_first",
        }

    def select(self, parameters: dict) -> dict:
        confirmation = self.tools.issue_selection_confirmation(
            self.token,
            agent_session_id=self.session,
            parameters=parameters,
        )
        return self.tools.select_creative_strategy(
            self.token,
            agent_session_id=self.session,
            parameters=parameters,
            confirmation_token=confirmation["confirmation_token"],
        )


class CreativeStrategyToolTests(unittest.TestCase):
    def test_get_uses_real_ai_spec_package_and_content_addressed_portfolio(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = StrategyToolFixture(Path(tmp))
            with (
                patch(
                    "mvp_v2.creative_strategy_tools.package_script_catalog",
                    wraps=package_script_catalog,
                ) as catalog,
                patch("mvp_v2.text_providers.build_text_provider") as provider,
            ):
                first = fixture.get()
                second = fixture.get()

            self.assertEqual(first["status"], "completed")
            self.assertEqual(first["result"]["portfolio"]["status"], "pass")
            self.assertEqual(first["result"]["provider_calls"], 0)
            self.assertEqual(
                first["result"]["portfolio_ref"], second["result"]["portfolio_ref"]
            )
            self.assertEqual(catalog.call_count, 2)
            self.assertTrue(all(
                call.kwargs["source_view"] == "ai-spec"
                for call in catalog.call_args_list
            ))
            provider.assert_not_called()
            stored = fixture.portfolios.read(first["result"]["portfolio_ref"])
            self.assertEqual(
                stored["bindings"]["brief_sha256"],
                fixture.projects.get_current_brief_snapshot(
                    fixture.token, fixture.project["project_id"]
                )["brief_sha256"],
            )
            self.assertEqual(stored["bindings"]["package"]["source_view"], "ai-spec")
            serialized = json.dumps(first, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(str(Path(tmp)), serialized)

    def test_select_preserves_brief_facts_recompiles_p00_and_replays_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = StrategyToolFixture(Path(tmp))
            portfolio = fixture.get()
            parameters = fixture.selection_parameters(portfolio)
            with patch("mvp_v2.text_providers.build_text_provider") as provider:
                selected = fixture.select(parameters)
                replayed = fixture.tools.select_creative_strategy(
                    fixture.token,
                    agent_session_id=fixture.session,
                    parameters=parameters,
                    confirmation_token="already-consumed-token-does-not-matter",
                )

            self.assertEqual(selected["status"], "completed")
            self.assertEqual(selected["result"]["selection"]["recompile_audit"]["status"], "pass")
            self.assertEqual(selected["result"]["provider_calls"], 0)
            self.assertTrue(replayed["replayed"])
            self.assertEqual(replayed["task_id"], selected["task_id"])
            self.assertEqual(replayed["result"], selected["result"])
            provider.assert_not_called()

            current = fixture.projects.get_current_brief_snapshot(
                fixture.token, fixture.project["project_id"]
            )
            derived = fixture.briefs.read(current["brief_ref"], current["brief_sha256"])
            for key, value in fixture.original_brief.items():
                if key != "planning_events":
                    self.assertEqual(derived[key], value, key)
            self.assertEqual(
                derived["planning_events"],
                selected["result"]["selection"]["planning_events"],
            )
            binding = derived["creative_strategy_selection"]
            self.assertEqual(binding["portfolio_ref"], portfolio["result"]["portfolio_ref"])
            self.assertEqual(binding["binding"]["strategy_id"], "focus_first")
            self.assertEqual(binding["provider_calls"], 0)
            template, _creative, _source = package_script_catalog(
                fixture.manifest,
                expected_category_id="launch",
                source_view="ai-spec",
            )
            recompiled = compile_plan_artifact(
                derived, template, source_view="ai-spec"
            )
            self.assertEqual(recompiled.audit["status"], "pass")
            with fixture.store.connection() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM project_brief_snapshots WHERE project_id = ?",
                    (fixture.project["project_id"],),
                ).fetchone()[0]
            self.assertEqual(count, 2)

    def test_session_project_plan_and_version_are_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = StrategyToolFixture(Path(tmp))
            with self.assertRaises(WorkspaceServiceError) as project_error:
                fixture.tools.get_creative_strategies(
                    fixture.token,
                    agent_session_id=fixture.other_session,
                    parameters={"project_id": fixture.project["project_id"]},
                )
            self.assertEqual(project_error.exception.code, "PERMISSION_DENIED")

            portfolio = fixture.get()
            bad_plan = fixture.selection_parameters(portfolio, key="bad-plan-key-0001")
            bad_plan["plan_sha256"] = "0" * 64
            with self.assertRaises(WorkspaceServiceError) as plan_error:
                fixture.tools.issue_selection_confirmation(
                    fixture.token,
                    agent_session_id=fixture.session,
                    parameters=bad_plan,
                )
            self.assertEqual(plan_error.exception.code, "INVALID_INPUT")
            with self.assertRaises(WorkspaceServiceError) as direct_plan_error:
                fixture.tools.select_creative_strategy(
                    fixture.token,
                    agent_session_id=fixture.session,
                    parameters=bad_plan,
                    confirmation_token="bypassed-confirmation-token",
                )
            self.assertEqual(direct_plan_error.exception.code, "INVALID_INPUT")
            with fixture.store.connection() as connection:
                idempotency_count = connection.execute(
                    "SELECT COUNT(*) FROM tool_idempotency_records"
                ).fetchone()[0]
                snapshot_count = connection.execute(
                    "SELECT COUNT(*) FROM project_brief_snapshots WHERE project_id = ?",
                    (fixture.project["project_id"],),
                ).fetchone()[0]
            self.assertEqual(idempotency_count, 0)
            self.assertEqual(snapshot_count, 1)

            fresh = fixture.briefs.freeze({**fixture.original_brief, "备注": "受控变更"})
            fixture.projects.add_brief_snapshot(
                fixture.token,
                project_id=fixture.project["project_id"],
                brief_ref=fresh["brief_ref"],
                brief_sha256=fresh["brief_sha256"],
                expected_project_version=portfolio["result"]["project_version"],
            )
            stale = fixture.selection_parameters(portfolio, key="stale-version-0001")
            with self.assertRaises(WorkspaceServiceError) as version_error:
                fixture.tools.issue_selection_confirmation(
                    fixture.token,
                    agent_session_id=fixture.session,
                    parameters=stale,
                )
            self.assertEqual(version_error.exception.code, "PROJECT_VERSION_CONFLICT")
            with self.assertRaises(WorkspaceServiceError) as direct_version_error:
                fixture.tools.select_creative_strategy(
                    fixture.token,
                    agent_session_id=fixture.session,
                    parameters=stale,
                    confirmation_token="bypassed-confirmation-token",
                )
            self.assertEqual(
                direct_version_error.exception.code, "PROJECT_VERSION_CONFLICT"
            )
            with fixture.store.connection() as connection:
                idempotency_count = connection.execute(
                    "SELECT COUNT(*) FROM tool_idempotency_records"
                ).fetchone()[0]
                snapshot_count = connection.execute(
                    "SELECT COUNT(*) FROM project_brief_snapshots WHERE project_id = ?",
                    (fixture.project["project_id"],),
                ).fetchone()[0]
            self.assertEqual(idempotency_count, 0)
            self.assertEqual(snapshot_count, 2)

    def test_clarification_portfolio_cannot_be_confirmed_or_claim_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = StrategyToolFixture(Path(tmp))
            pending = deepcopy(fixture.original_brief)
            pending["planning_events"][1]["fact_status"] = "pending_confirmation"
            pending["planning_events"][1]["evidence"] = "嘉宾身份待确认"
            frozen = fixture.briefs.freeze(pending)
            updated = fixture.projects.add_brief_snapshot(
                fixture.token,
                project_id=fixture.project["project_id"],
                brief_ref=frozen["brief_ref"],
                brief_sha256=frozen["brief_sha256"],
                expected_project_version=0,
            )
            portfolio = fixture.get()
            self.assertEqual(
                portfolio["result"]["portfolio"]["status"],
                "needs_clarification",
            )
            parameters = fixture.selection_parameters(
                portfolio, key="pending-strategy-0001"
            )
            parameters["expected_project_version"] = updated["project_version"]
            with self.assertRaises(WorkspaceServiceError) as confirmation_error:
                fixture.tools.issue_selection_confirmation(
                    fixture.token,
                    agent_session_id=fixture.session,
                    parameters=parameters,
                )
            self.assertEqual(confirmation_error.exception.code, "REVIEW_REQUIRED")
            with self.assertRaises(WorkspaceServiceError) as direct_error:
                fixture.tools.select_creative_strategy(
                    fixture.token,
                    agent_session_id=fixture.session,
                    parameters=parameters,
                    confirmation_token="bypassed-confirmation-token",
                )
            self.assertEqual(direct_error.exception.code, "REVIEW_REQUIRED")
            with fixture.store.connection() as connection:
                idempotency_count = connection.execute(
                    "SELECT COUNT(*) FROM tool_idempotency_records"
                ).fetchone()[0]
                snapshot_count = connection.execute(
                    "SELECT COUNT(*) FROM project_brief_snapshots WHERE project_id = ?",
                    (fixture.project["project_id"],),
                ).fetchone()[0]
            self.assertEqual(idempotency_count, 0)
            self.assertEqual(snapshot_count, 2)

    def test_portfolio_write_io_failure_finishes_r1_task_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = StrategyToolFixture(Path(tmp))
            with patch(
                "mvp_v2.creative_strategy_artifacts._write_once",
                side_effect=OSError("read-only volume"),
            ):
                result = fixture.get()

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "INTERNAL_ERROR")
            self.assertEqual(
                result["error"]["details"]["source_code"],
                "ARTIFACT_WRITE_FAILED",
            )
            task = fixture.tasks.get_task(fixture.token, result["task_id"])
            self.assertEqual(task["status"], "failed")

    def test_crash_after_snapshot_write_blocks_automatic_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def checkpoint(name: str) -> None:
                if name == "after_domain_write":
                    raise RuntimeError("simulated crash")

            fixture = StrategyToolFixture(root, checkpoint=checkpoint)
            portfolio = fixture.get()
            parameters = fixture.selection_parameters(
                portfolio, key="strategy-crash-0001"
            )
            confirmation = fixture.tools.issue_selection_confirmation(
                fixture.token,
                agent_session_id=fixture.session,
                parameters=parameters,
            )
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                fixture.tools.select_creative_strategy(
                    fixture.token,
                    agent_session_id=fixture.session,
                    parameters=parameters,
                    confirmation_token=confirmation["confirmation_token"],
                )
            with self.assertRaises(CreativeStrategyRecoveryRequired):
                fixture.tools.select_creative_strategy(
                    fixture.token,
                    agent_session_id=fixture.session,
                    parameters=parameters,
                    confirmation_token=confirmation["confirmation_token"],
                )
            with fixture.store.connection() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM project_brief_snapshots WHERE project_id = ?",
                    (fixture.project["project_id"],),
                ).fetchone()[0]
            self.assertEqual(count, 2)

    def test_portfolio_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = StrategyToolFixture(Path(tmp))
            portfolio = fixture.get()
            ref = portfolio["result"]["portfolio_ref"]
            digest = ref.rsplit("/", 1)[-1]
            path = fixture.portfolios.root / digest[:2] / f"{digest}.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(WorkspaceServiceError) as caught:
                fixture.portfolios.read(ref)
            self.assertEqual(caught.exception.code, "ARTIFACT_HASH_MISMATCH")


def _write_source_workbook(path: Path) -> None:
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    headers = [
        "结构",
        "镜号",
        "形式",
        "效果",
        "景别",
        "运镜",
        "画面描述",
        "声音",
        "备注",
        "镜头功能",
        "画面主体",
        "可复用等级",
        "禁用说明",
    ]
    for column, value in enumerate(headers, start=1):
        template.cell(6, column, value)
    for index in range(1, 4):
        row = index + 6
        values = [
            "建立" if index == 1 else "推进" if index == 2 else "收束",
            str(index),
            "实拍",
            "",
            "全景" if index == 1 else "中景",
            "推进",
            f"匿名功能镜头 {index}",
            "现场声",
            "匿名 fixture",
            f"匿名镜头功能 {index}",
            f"匿名事件主体 {index}",
            "镜头功能可复用",
            "",
        ]
        for column, value in enumerate(values, start=1):
            template.cell(row, column, value)
    creative = workbook.create_sheet("创意库")
    for column, value in enumerate(headers, start=1):
        creative.cell(6, column, value)
    values = [
        "推进",
        "C001",
        "实拍",
        "",
        "中景",
        "跟随",
        "匿名创意镜头",
        "现场声",
        "匿名 fixture",
        "匿名创意功能",
        "匿名事件主体",
        "镜头功能可复用",
        "",
    ]
    for column, value in enumerate(values, start=1):
        creative.cell(7, column, value)
    workbook.save(path)
    workbook.close()


if __name__ == "__main__":
    unittest.main()
