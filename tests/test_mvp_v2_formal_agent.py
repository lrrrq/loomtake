from __future__ import annotations

import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentServices
from mvp_v2.formal_advisor import FormalAdvisorDecision, FormalAdvisorPolicy, FormalAdvisorService
from mvp_v2.formal_agent import FormalExecutableAgentService, FormalExecutionBackend
from mvp_v2.formal_admin_service import FormalAdminService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceStore
from mvp_v2.workspace_store import WorkspaceServiceError


class FakeFormalGeneration:
    def __init__(self, *, has_plan=False, needs_clarification=False):
        self.prepare_count = 0
        self.generate_count = 0
        self.has_plan = has_plan
        self.needs_clarification = needs_clarification
        self.plan_id = "task_formalplan12345678"
        self.plan_sha256 = "a" * 64

    def current_confirmed_brief(self, *_args, **_kwargs):
        return SimpleNamespace(brief_version_id="brief_version_12345678")

    def list_waiting_plans(self, *_args, **_kwargs):
        return {
            "items": [{"plan_id": self.plan_id, "status": "waiting_user"}]
            if self.has_plan else []
        }

    def get_plan_preview(self, *_args, **_kwargs):
        return {
            "plan_id": self.plan_id,
            "needs_clarification": self.needs_clarification,
            "audit": {"status": "needs_clarification" if self.needs_clarification else "pass"},
            "coverage": [{
                "requirement_id": "req_12345678", "kind": "required",
                "status": "covered", "shot_nos": [1], "evidence_refs": ["brief:line:1"],
            }],
            "capacity_report": {
                "status": "needs_clarification" if self.needs_clarification else "ready",
            },
            "traceability": [{
                "requirement_id": "req_12345678", "brief_text": "匿名强制要求",
                "plan_shot_nos": ["1"], "final_status": "covered",
            }],
        }

    def _plan_event(self, *_args, **_kwargs):
        return {"plan_sha256": self.plan_sha256, "plan_ref": "artifact://run/plan/script_plan"}

    def prepare_plan(self, *_args, **_kwargs):
        self.prepare_count += 1
        self.has_plan = True
        return {
            "plan_id": self.plan_id,
            "needs_clarification": False,
        }

    def confirm_plan_and_generate(self, *_args, **_kwargs):
        self.generate_count += 1
        return {
            "version_id": "version_12345678",
            "pending_confirmations": ["人工确认品牌授权"],
        }


class StaticAdvisor:
    def __init__(self, intent):
        self.intent = intent

    def decide(self, _message, context):
        action = {
            "prepare_plan": "prepare_formal_plan",
            "confirm_generation": "confirm_formal_plan_generation",
        }.get(self.intent, "none")
        return FormalAdvisorDecision(
            intent=self.intent,
            summary="基于当前项目证据生成受控提案。",
            evidence_ids=context["evidence_ids"],
            questions=[],
            candidate_action=action,
        )


class FormalAgentFixture:
    def __init__(
        self, root: Path, *, advisor=None, writes_enabled=True,
        generation=None,
    ):
        self.root = root
        self.store = WorkspaceStore(root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区", display_name="本机管理员",
            confirmed=True, token="formal-owner-token",
        )
        self.token = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.tasks = TaskService(self.store)
        self.project = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名明星活动",
            category_id="star_activity", expected_workspace_version=0,
        )
        self.session = self.tasks.create_agent_session(
            self.token, project_id=self.project["project_id"],
        )
        readonly = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store,
            identity=self.identity,
            projects=self.projects,
            tasks=self.tasks,
            versions=VersionService(self.store),
            artifacts=RunArtifactResolver(root / "runs"),
        ))
        self.generation = generation or FakeFormalGeneration()
        self.services = ExecutableAgentServices(
            readonly=readonly,
            projects=self.projects,
            tasks=self.tasks,
            confirmations=ActionConfirmationService(self.store, b"f" * 32),
            idempotency=ToolIdempotencyService(self.store),
            brief_store=BriefArtifactStore(root / "data"),
            backend=FormalExecutionBackend(),
        )
        self.agent = FormalExecutableAgentService(
            self.services, generation=self.generation,
            advisor=advisor, writes_enabled=writes_enabled,
            operations=FormalAdminService(self.store),
        )

    def submit(self, content):
        return self.agent.submit_message(
            self.token, self.session["agent_session_id"], content=content,
        )

    def confirm(self, proposal):
        return self.agent.confirm_action(
            self.token, proposal["task"]["task_id"],
            proposal_digest=proposal["response"]["proposal_digest"],
        )


class FormalAgentTests(unittest.TestCase):
    def test_question_bound_short_answer_preserves_prior_sources_and_replays(self):
        from io import BytesIO
        from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
        from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
        from mvp_v2.creative_revision import effective_brief_values
        with tempfile.TemporaryDirectory() as tmp:
            advisor = SimpleNamespace(decide=lambda *_: FormalAdvisorDecision(intent='needs_clarification',
                summary='请补充客户品牌。', questions=['这次客户品牌是什么？'],
                question_fields=['品牌或客户'], candidate_action='none'))
            fixture = FormalAgentFixture(Path(tmp), advisor=advisor)
            adapter = FormalBriefInputAdapter(FormalBriefWorkspaceService(fixture.store,
                artifact_root=Path(tmp) / 'data'), data_root=Path(tmp) / 'data')
            fixture.agent.script_revision = SimpleNamespace(brief_adapter=adapter)
            scope = {'workspace_id': fixture.workspace_id, 'project_id': fixture.project['project_id']}
            adapter.import_uploads(fixture.token, **scope, expected_project_version=0,
                uploads=[UploadedDocument('需求.txt', BytesIO('脚本类型：年会\n视频目标：凝聚团队'.encode()), True)])
            session = fixture.session['agent_session_id']
            analysis = fixture.agent.analyze_brief(fixture.token, session, content='还需要什么？')
            binding = analysis['response']['question_bindings'][0]
            project = fixture.projects.get_project(fixture.token, scope['project_id'])
            args = dict(question_id=binding['question_id'], analysis_task_id=binding['analysis_task_id'],
                content='匿名客户', expected_project_version=project['project_version'])
            answered = fixture.agent.answer_brief_question(fixture.token, session, **args)
            current = adapter.service.read_current_formal(fixture.token, **scope)
            values = effective_brief_values(current)
            self.assertEqual(values['品牌或客户'], '匿名客户')
            self.assertEqual(values['视频目标'], '凝聚团队')
            self.assertEqual(len(current['brief']['source_manifest']['documents']), 2)
            replay = fixture.agent.answer_brief_question(fixture.token, session, **args)
            self.assertEqual(answered['brief_key'], replay['brief_key'])
            with self.assertRaises(WorkspaceServiceError):
                fixture.agent.answer_brief_question(fixture.token, session, **{**args, 'question_id': 'invented'})

    def test_requirement_analysis_uses_brief_history_and_cannot_dispatch_generation(self):
        from io import BytesIO
        from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
        from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp), advisor=StaticAdvisor("explain_status"))
            adapter = FormalBriefInputAdapter(FormalBriefWorkspaceService(fixture.store,
                artifact_root=Path(tmp) / "data"), data_root=Path(tmp) / "data")
            fixture.agent.script_revision = SimpleNamespace(brief_adapter=adapter)
            adapter.import_uploads(fixture.token, workspace_id=fixture.workspace_id,
                project_id=fixture.project["project_id"], expected_project_version=0,
                uploads=[UploadedDocument("匿名需求.txt", BytesIO("脚本类型：年会\n风格调性：克制\n视频目标：凝聚团队".encode()), True)])
            session_id = fixture.session["agent_session_id"]
            result = fixture.agent.analyze_brief(fixture.token, session_id, content="分析需求，先不要写稿")
            self.assertEqual(result["task"]["status"], "completed")
            reread = fixture.agent.get_session(fixture.token, session_id)
            self.assertEqual(reread["timeline"][-1]["response"]["user_message"], "分析需求，先不要写稿")
            fixture.agent.advisor = StaticAdvisor("confirm_generation")
            invalid = fixture.agent.analyze_brief(fixture.token, session_id, content="现在直接生成")
            self.assertEqual(invalid["task"]["status"], "failed")
            self.assertEqual(invalid["response"]["error_code"], "BRIEF_ANALYSIS_INVALID")
            self.assertEqual(fixture.generation.generate_count, 0)
            self.assertEqual(fixture.generation.prepare_count, 0)
            with fixture.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM brief_confirmations").fetchone()[0], 0)

    @staticmethod
    def _executing_formal_task(fixture, action, parameters):
        task = fixture.tasks.create_task(
            fixture.token,
            agent_session_id=fixture.session["agent_session_id"],
            task_type="formal_production_advisor",
            payload_sha256=(action + "-crash-reconcile").encode().hex().ljust(64, "0")[:64],
            expected_project_version=fixture.project["project_version"],
        )
        task = fixture.tasks.transition_task(
            fixture.token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="understanding",
            details={"formal_context_version": fixture.project["project_version"]},
        )
        task = fixture.tasks.transition_task(
            fixture.token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="proposed",
            details={
                "response": {
                    "type": "action_proposal", "action": action,
                    "proposal_digest": "a" * 64,
                },
                "internal_formal_parameters": parameters,
            },
        )
        task = fixture.tasks.transition_task(
            fixture.token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="waiting_confirmation",
            details={},
        )
        return fixture.tasks.transition_task(
            fixture.token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="executing",
            details={"execution_task_id": task["task_id"]},
        )

    @staticmethod
    def _waiting_plan_task(fixture, *, operation_key, generation_mode):
        session = fixture.tasks.create_agent_session(
            fixture.token, project_id=fixture.project["project_id"],
        )
        task = fixture.tasks.create_task(
            fixture.token,
            agent_session_id=session["agent_session_id"],
            task_type="formal_generation_plan",
            payload_sha256="f" * 64,
            expected_project_version=fixture.project["project_version"],
        )
        task = fixture.tasks.transition_task(
            fixture.token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="understanding", details={},
        )
        task = fixture.tasks.transition_task(
            fixture.token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="executing",
            details={
                "action": "prepare_plan", "operation_key": operation_key,
                "outer_task_id": operation_key,
                "project_id": fixture.project["project_id"],
                "expected_project_version": fixture.project["project_version"],
            },
        )
        return fixture.tasks.transition_task(
            fixture.token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="waiting_user",
            details={
                "action": "prepare_plan", "generation_mode": generation_mode,
                "operation_key": operation_key, "outer_task_id": operation_key,
                "project_id": fixture.project["project_id"],
                "expected_project_version": fixture.project["project_version"],
                "plan_ref": "artifact://run/plan/script_plan",
                "plan_sha256": "1" * 64,
            },
        )

    def test_four_image_intents_are_strictly_classified_without_trusting_image_identity(self):
        cases = {
            "查看第 3 镜当前图片和候选": "inspect_image_candidates",
            "把第 3 镜换成刚上传的产品图": "propose_image_revision",
            "给第2镜换一张图片": "propose_image_revision",
            "第2镜重新配图": "propose_image_revision",
            "第 3 镜当前图片不合适，排除后重新找": "propose_image_feedback",
            "第 5 镜暂准文字依据，客户现场图待确认": "propose_temporary_text_basis",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                intent, shot_no, _query = FormalExecutableAgentService._strict_image_intent(message)
                self.assertEqual((intent, shot_no), (expected, re.search(r"\d+", message).group(0)))
        ambiguous = FormalExecutableAgentService._strict_image_intent(
            "查看第 3 镜和第 4 镜当前图片候选",
        )
        self.assertEqual(ambiguous[1], "")

    def test_ordinary_stage_retry_creates_attempt_but_force_recovery_only_hands_off_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp), advisor=None, writes_enabled=False)
            failed = fixture.tasks.create_task(
                fixture.token,
                agent_session_id=fixture.session["agent_session_id"],
                task_type="machine_audit",
                payload_sha256="d" * 64,
                expected_project_version=fixture.project["project_version"],
            )
            fixture.tasks.transition_task(
                fixture.token,
                task_id=failed["task_id"],
                expected_task_version=failed["task_version"],
                status="failed",
                details={"stage_id": "machine_audit"},
                error_code="PROVIDER_TIMEOUT",
            )

            proposal = fixture.submit("重试 machine_audit 节点")
            self.assertEqual(proposal["response"]["action"], "retry_formal_stage")
            self.assertEqual(proposal["task"]["status"], "proposed")
            completed = fixture.confirm(proposal)
            self.assertEqual(completed["task"]["status"], "completed")
            self.assertFalse(completed["response"]["admin_recovery_used"])

            force = fixture.submit("强制重试 machine_audit 并从 checkpoint 恢复")
            self.assertEqual(force["task"]["status"], "completed")
            self.assertEqual(force["response"]["type"], "admin_handoff")
            self.assertEqual(force["response"]["admin_path"], "/admin")
            with fixture.store.connection() as connection:
                attempts = connection.execute(
                    "SELECT attempt_no FROM task_attempts WHERE task_id=? ORDER BY attempt_no",
                    (failed["task_id"],),
                ).fetchall()
                admin_proposals = connection.execute(
                    "SELECT COUNT(*) FROM recovery_proposals WHERE action<>'retry_current_stage'",
                ).fetchone()[0]
            self.assertEqual([row["attempt_no"] for row in attempts], [1, 2])
            self.assertEqual(admin_proposals, 0)

    def test_missing_confirmed_brief_is_explained_and_never_becomes_a_write_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp), advisor=None, writes_enabled=False)

            def missing(*_args, **_kwargs):
                raise WorkspaceServiceError(
                    "CONFIRMED_BRIEF_REQUIRED", "当前项目尚无已确认 Brief",
                )

            fixture.generation.current_confirmed_brief = missing
            status = fixture.submit("当前状态和阻断原因")
            self.assertEqual(status["task"]["status"], "completed")
            self.assertEqual(
                status["response"]["blockers"][0]["code"],
                "CONFIRMED_BRIEF_REQUIRED",
            )

            fixture.agent.advisor = StaticAdvisor("prepare_plan")
            fixture.agent.writes_enabled = True
            write = fixture.submit("准备规划")
            self.assertEqual(write["task"]["status"], "blocked")
            self.assertEqual(write["task"]["error_code"], "CONFIRMED_BRIEF_REQUIRED")
            self.assertEqual(fixture.generation.prepare_count, 0)

    def test_provider_unavailable_and_rollout_disabled_block_all_business_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            unavailable = FormalAgentFixture(
                Path(tmp) / "unavailable", advisor=None, writes_enabled=False,
            )
            blocked = unavailable.submit("请准备规划")
            self.assertEqual(blocked["task"]["status"], "blocked")
            self.assertEqual(blocked["task"]["error_code"], "FORMAL_ADVISOR_UNAVAILABLE")
            self.assertEqual(unavailable.generation.prepare_count, 0)
            self.assertEqual(unavailable.generation.generate_count, 0)

            disabled = FormalAgentFixture(
                Path(tmp) / "disabled", advisor=StaticAdvisor("prepare_plan"),
                writes_enabled=False,
            )
            rollout = disabled.submit("请准备规划")
            self.assertEqual(rollout["task"]["status"], "blocked")
            self.assertEqual(rollout["task"]["error_code"], "FORMAL_AGENT_WRITES_DISABLED")
            self.assertEqual(disabled.generation.prepare_count, 0)

    def test_missing_advisor_does_not_block_deterministic_formal_plan_preparation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp), advisor=None)
            proposal = fixture.submit("按只生成文字草稿模式准备规划")

            self.assertEqual(proposal["task"]["status"], "proposed")
            self.assertEqual(proposal["response"]["action"], "prepare_formal_plan")
            self.assertEqual(proposal["response"]["confirmation_stage"], "confirmation_required")
            self.assertEqual(proposal["response"]["next_href"], f"/projects/{fixture.project['project_id']}/plan")
            completed = fixture.confirm(proposal)
            self.assertEqual(completed["task"]["status"], "completed")
            self.assertEqual(fixture.generation.prepare_count, 1)

    def test_formal_image_confirmation_persists_executing_before_authority_call(self):
        observed_statuses = []
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp), advisor=None)
            parameters = {
                "project_id": fixture.project["project_id"],
                "base_version_id": "version_12345678",
                "expected_project_version": fixture.project["project_version"],
                "image_proposal_id": "task_12345678",
                "image_proposal_digest": "a" * 64,
                "target_shot_no": "1",
                "target_image_id": "media_12345678",
                "candidate_task_id": "task_12345678",
                "idempotency_key": "image-confirmation-1",
            }
            task = fixture.tasks.create_task(
                fixture.token,
                agent_session_id=fixture.session["agent_session_id"],
                task_type="formal_image_revision",
                payload_sha256="e" * 64,
                expected_project_version=fixture.project["project_version"],
            )
            task = fixture.tasks.transition_task(
                fixture.token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="understanding",
                details={},
            )
            digest = "b" * 64
            task = fixture.tasks.transition_task(
                fixture.token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="proposed",
                details={
                    "response": {
                        "type": "action_proposal",
                        "action": "apply_formal_image_revision",
                        "proposal_digest": digest,
                    },
                    "internal_formal_parameters": parameters,
                },
            )

            def execute_image(_auth_token, *, project, parameters):
                del project, parameters
                observed_statuses.append(
                    fixture.tasks.get_task(fixture.token, task["task_id"])["status"]
                )
                return {"type": "formal_image_revision_completed", "status": "completed"}

            fixture.agent.script_revision = SimpleNamespace(
                writes_enabled=True, execute_image=execute_image,
            )
            result = fixture.agent.confirm_action(
                fixture.token, task["task_id"], proposal_digest=digest,
            )

            self.assertEqual(observed_statuses, ["executing"])
            self.assertEqual(result["task"]["status"], "completed")

    def test_restart_reconciles_existing_formal_plan_without_creating_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp), advisor=StaticAdvisor("prepare_plan"))
            proposal = fixture.submit("按只生成文字草稿模式准备规划")
            outer = fixture.tasks.get_task(fixture.token, proposal["task"]["task_id"])
            event = next(
                event for event in reversed(fixture.tasks.list_task_events(fixture.token, outer["task_id"]))
                if event["details"].get("internal_formal_parameters")
            )
            parameters = event["details"]["internal_formal_parameters"]
            claim = fixture.services.idempotency.claim(
                fixture.token,
                agent_session_id=outer["agent_session_id"],
                tool_name="prepare_formal_plan",
                parameters=parameters,
            )
            confirmation = fixture.services.confirmations.issue(
                fixture.token,
                agent_session_id=outer["agent_session_id"],
                tool_name="prepare_formal_plan",
                parameters=parameters,
            )
            fixture.services.confirmations.consume(
                fixture.token,
                confirmation_token=confirmation["confirmation_token"],
                agent_session_id=outer["agent_session_id"],
                tool_name="prepare_formal_plan",
                parameters=parameters,
            )
            outer = fixture.tasks.transition_task(
                fixture.token, task_id=outer["task_id"],
                expected_task_version=outer["task_version"], status="waiting_confirmation",
                details={"idempotency_record_id": claim["idempotency_record_id"]},
            )
            fixture.tasks.transition_task(
                fixture.token, task_id=outer["task_id"],
                expected_task_version=outer["task_version"], status="executing",
                details={
                    "idempotency_record_id": claim["idempotency_record_id"],
                    "execution_task_id": outer["task_id"],
                },
            )

            inner_session = fixture.tasks.create_agent_session(
                fixture.token, project_id=fixture.project["project_id"],
            )
            inner = fixture.tasks.create_task(
                fixture.token,
                agent_session_id=inner_session["agent_session_id"],
                task_type="formal_generation_plan",
                payload_sha256="f" * 64,
                expected_project_version=fixture.project["project_version"],
            )
            inner = fixture.tasks.transition_task(
                fixture.token, task_id=inner["task_id"],
                expected_task_version=inner["task_version"], status="understanding", details={},
            )
            inner = fixture.tasks.transition_task(
                fixture.token, task_id=inner["task_id"],
                expected_task_version=inner["task_version"], status="executing", details={},
            )
            fixture.tasks.transition_task(
                fixture.token, task_id=inner["task_id"],
                expected_task_version=inner["task_version"], status="waiting_user",
                details={
                    "action": "prepare_plan",
                    "generation_mode": "text_only_draft",
                    "operation_key": outer["task_id"],
                    "outer_task_id": outer["task_id"],
                    "project_id": fixture.project["project_id"],
                    "expected_project_version": fixture.project["project_version"],
                    "plan_ref": "artifact://run/plan/script_plan",
                    "plan_sha256": "1" * 64,
                },
            )

            recovered = fixture.agent.sync_task(fixture.token, outer["task_id"])

            self.assertEqual(recovered["task"]["status"], "completed")
            self.assertEqual(recovered["response"]["plan_id"], inner["task_id"])
            self.assertEqual(fixture.generation.prepare_count, 0)
            with fixture.store.connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM tasks WHERE task_type='formal_generation_plan'"
                    ).fetchone()[0],
                    1,
                )

    def test_prepare_plan_reconciliation_does_not_claim_another_outer_task_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp), advisor=StaticAdvisor("prepare_plan"))
            outer = fixture.tasks.create_task(
                fixture.token,
                agent_session_id=fixture.session["agent_session_id"],
                task_type="formal_production_advisor",
                payload_sha256="e" * 64,
                expected_project_version=fixture.project["project_version"],
            )
            outer = fixture.tasks.transition_task(
                fixture.token, task_id=outer["task_id"],
                expected_task_version=outer["task_version"], status="understanding", details={},
            )
            parameters = {
                "project_id": fixture.project["project_id"],
                "expected_project_version": fixture.project["project_version"],
                "generation_mode": "text_only_draft",
            }
            outer = fixture.tasks.transition_task(
                fixture.token, task_id=outer["task_id"],
                expected_task_version=outer["task_version"], status="proposed",
                details={
                    "response": {
                        "type": "action_proposal", "action": "prepare_formal_plan",
                        "proposal_digest": "a" * 64,
                    },
                    "internal_formal_parameters": parameters,
                },
            )
            outer = fixture.tasks.transition_task(
                fixture.token, task_id=outer["task_id"],
                expected_task_version=outer["task_version"], status="waiting_confirmation", details={},
            )
            outer = fixture.tasks.transition_task(
                fixture.token, task_id=outer["task_id"],
                expected_task_version=outer["task_version"], status="executing", details={},
            )
            self._waiting_plan_task(
                fixture, operation_key="another-outer-task", generation_mode="text_only_draft",
            )

            recovered = fixture.agent.sync_task(fixture.token, outer["task_id"])

            self.assertEqual(recovered["task"]["status"], "blocked")
            self.assertEqual(
                recovered["task"]["error_code"],
                "RECOVERY_RECONCILIATION_REQUIRED",
            )
            with fixture.store.connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM tasks WHERE task_type='formal_generation_plan'"
                    ).fetchone()[0],
                    1,
                )

    def test_proposal_is_server_rebuilt_and_confirmation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(
                Path(tmp), advisor=StaticAdvisor("prepare_plan"),
            )
            proposal = fixture.submit("请从 /tmp/forged 为其他项目按只生成文字草稿模式准备规划")
            public = json.dumps(proposal["response"], ensure_ascii=False)

            self.assertEqual(proposal["task"]["status"], "proposed")
            self.assertEqual(proposal["response"]["action"], "prepare_formal_plan")
            self.assertNotIn("parameters", proposal["response"])
            self.assertNotIn("/tmp/forged", public)
            self.assertNotIn("idempotency_key", public)
            self.assertEqual(fixture.generation.prepare_count, 0)

            first = fixture.confirm(proposal)
            second_proposal = fixture.submit("请再按只生成文字草稿模式准备一次规划")
            second = fixture.confirm(second_proposal)

            self.assertEqual(first["response"]["status"], "waiting_user")
            self.assertEqual(first["response"]["draft_status"], "waiting_user")
            self.assertEqual(first["response"]["confirmation_stage"], "plan_review")
            self.assertEqual(second["task"]["task_id"], first["task"]["task_id"])
            self.assertEqual(fixture.generation.prepare_count, 1)
            with fixture.store.connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM action_confirmations").fetchone()[0],
                    1,
                )

    def test_forged_evidence_or_sensitive_model_output_never_reaches_business_write(self):
        secret = "deepseek-formal-agent-secret"

        def forged(*_args):
            return json.dumps({
                "intent": "prepare_plan", "summary": "伪造证据",
                "evidence_ids": ["project:project_other0000:v9"],
                "questions": [], "candidate_action": "prepare_formal_plan",
            })

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"DEEPSEEK_API_KEY": secret},
        ):
            advisor = FormalAdvisorService(FormalAdvisorPolicy(
                invoke=forged, provider="deepseek-http", model="deepseek-v4-flash",
            ))
            fixture = FormalAgentFixture(Path(tmp), advisor=advisor)
            result = fixture.submit("准备规划")
            self.assertEqual(result["task"]["status"], "needs_clarification")
            self.assertEqual(result["response"]["error_code"], "FORMAL_ADVISOR_EVIDENCE_INVALID")
            self.assertEqual(fixture.generation.prepare_count, 0)
            self.assertNotIn(secret.encode(), fixture.store.database_path.read_bytes())

    def test_project_or_plan_sha_drift_invalidates_old_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_drift = FormalAgentFixture(
                Path(tmp) / "project", advisor=StaticAdvisor("prepare_plan"),
            )
            proposal = project_drift.submit("按只生成文字草稿模式准备规划")
            project_drift.projects.rename_project(
                project_drift.token,
                project_id=project_drift.project["project_id"],
                name="匿名明星活动-已变化",
                expected_project_version=project_drift.project["project_version"],
            )
            stale = project_drift.confirm(proposal)
            self.assertEqual(stale["task"]["error_code"], "PROJECT_VERSION_CONFLICT")
            self.assertEqual(project_drift.generation.prepare_count, 0)

            generation = FakeFormalGeneration(has_plan=True)
            plan_drift = FormalAgentFixture(
                Path(tmp) / "plan", advisor=StaticAdvisor("confirm_generation"),
                generation=generation,
            )
            generation_proposal = plan_drift.submit("确认生成脚本")
            self.assertNotIn("parameters", generation_proposal["response"])
            self.assertNotIn(
                "plan_sha256",
                json.dumps(generation_proposal["response"], ensure_ascii=False),
            )
            generation.plan_sha256 = "b" * 64
            stale_plan = plan_drift.confirm(generation_proposal)
            self.assertEqual(stale_plan["task"]["error_code"], "PLAN_CONFIRMATION_MISMATCH")
            self.assertEqual(generation.generate_count, 0)

    def test_plan_and_generation_require_two_distinct_confirmations(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(
                Path(tmp), advisor=StaticAdvisor("prepare_plan"),
            )
            plan = fixture.confirm(fixture.submit("按只生成文字草稿模式准备规划"))
            self.assertEqual(plan["task"]["status"], "completed")

            fixture.agent.advisor = StaticAdvisor("confirm_generation")
            generation_proposal = fixture.submit("确认生成脚本")
            self.assertEqual(fixture.generation.generate_count, 0)
            generated = fixture.confirm(generation_proposal)

            self.assertEqual(generated["task"]["status"], "waiting_human_review")
            self.assertEqual(generated["response"]["summary"], "机器审核通过，等待人工复核。")
            self.assertEqual(fixture.generation.generate_count, 1)
            with fixture.store.connection() as connection:
                rows = connection.execute(
                    "SELECT tool_name, status FROM action_confirmations ORDER BY created_at, confirmation_id"
                ).fetchall()
            self.assertEqual(
                [(row["tool_name"], row["status"]) for row in rows],
                [
                    ("prepare_formal_plan", "consumed"),
                    ("confirm_formal_plan_generation", "consumed"),
                ],
            )

    def test_agent_reads_trace_but_cannot_propose_generation_during_clarification(self):
        with tempfile.TemporaryDirectory() as tmp:
            generation = FakeFormalGeneration(
                has_plan=True, needs_clarification=True,
            )
            fixture = FormalAgentFixture(
                Path(tmp), advisor=StaticAdvisor("explain_issue"),
                generation=generation,
            )
            explained = fixture.submit("解释当前冲突")
            self.assertEqual(explained["task"]["status"], "completed")
            self.assertEqual(
                explained["response"]["capacity_report"]["status"],
                "needs_clarification",
            )
            self.assertEqual(
                explained["response"]["traceability"][0]["requirement_id"],
                "req_12345678",
            )

            fixture.agent.advisor = StaticAdvisor("confirm_generation")
            blocked = fixture.submit("直接生成脚本")
            self.assertEqual(blocked["task"]["status"], "blocked")
            self.assertEqual(
                blocked["task"]["error_code"], "PLANNING_NEEDS_CLARIFICATION",
            )
            self.assertEqual(generation.generate_count, 0)

    def test_missing_runtime_handle_is_reported_as_reconciliation_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(
                Path(tmp), advisor=StaticAdvisor("prepare_plan"),
            )
            task = fixture.tasks.create_task(
                fixture.token,
                agent_session_id=fixture.session["agent_session_id"],
                task_type="formal_production_advisor",
                payload_sha256="c" * 64,
                expected_project_version=fixture.project["project_version"],
            )
            for status in ("understanding", "proposed", "waiting_confirmation", "executing"):
                task = fixture.tasks.transition_task(
                    fixture.token, task_id=task["task_id"],
                    expected_task_version=task["task_version"], status=status,
                    details={},
                )

            recovered = fixture.agent.sync_task(fixture.token, task["task_id"])
            self.assertEqual(recovered["task"]["status"], "blocked")
            self.assertEqual(
                recovered["task"]["error_code"],
                "RECOVERY_RECONCILIATION_REQUIRED",
            )

    def test_sync_does_not_claim_unrelated_child_with_same_parent_and_different_image_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp))
            parent_version_id = "version_formal_parent"
            child_version_id = "version_unrelated_child"
            parent_run_id = "run_formal_parent"
            child_run_id = "run_unrelated_child"
            parameters = {
                "project_id": fixture.project["project_id"],
                "base_version_id": parent_version_id,
                "expected_project_version": fixture.project["project_version"],
                "idempotency_key": "formal-image-current-task",
            }
            task = self._executing_formal_task(
                fixture, "apply_formal_image_revision", parameters,
            )
            with fixture.store.transaction() as connection:
                connection.execute(
                    """INSERT INTO project_runs(
                    run_id, project_id, parent_run_id, source_kind,
                    manifest_ref, manifest_sha256, bound_by_actor_id, bound_at)
                    VALUES (?, ?, NULL, 'native', ?, ?, ?, ?)""",
                    (
                        parent_run_id, fixture.project["project_id"],
                        "artifact://run/formal-parent/manifest", "1" * 64,
                        task["actor_id"], "2026-01-01T00:00:00Z",
                    ),
                )
                connection.execute(
                    """INSERT INTO project_versions(
                    version_id, project_id, run_id, parent_version_id,
                    content_sha256, artifact_ref, created_by_actor_id, created_at)
                    VALUES (?, ?, ?, NULL, ?, ?, ?, ?)""",
                    (
                        parent_version_id, fixture.project["project_id"], parent_run_id,
                        "2" * 64, "artifact://run/formal-parent/workbook",
                        task["actor_id"], "2026-01-01T00:00:00Z",
                    ),
                )
                connection.execute(
                    """INSERT INTO project_runs(
                    run_id, project_id, parent_run_id, source_kind,
                    manifest_ref, manifest_sha256, bound_by_actor_id, bound_at)
                    VALUES (?, ?, ?, 'native', ?, ?, ?, ?)""",
                    (
                        child_run_id, fixture.project["project_id"], parent_run_id,
                        "artifact://run/unrelated-child/manifest", "3" * 64,
                        task["actor_id"], "2026-01-01T00:00:01Z",
                    ),
                )
                connection.execute(
                    """INSERT INTO project_versions(
                    version_id, project_id, run_id, parent_version_id,
                    content_sha256, artifact_ref, created_by_actor_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        child_version_id, fixture.project["project_id"], child_run_id,
                        parent_version_id, "4" * 64,
                        "artifact://run/unrelated-child/workbook",
                        task["actor_id"], "2026-01-01T00:00:01Z",
                    ),
                )
                connection.execute(
                    """INSERT INTO version_image_decision_snapshots_v3 VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "snapshot_unrelated_key", fixture.workspace_id,
                        fixture.project["project_id"], parent_version_id,
                        child_version_id, child_run_id, 1, 1, 0,
                        "artifact://snapshot/unrelated-key", "5" * 64,
                        "6" * 64, "7" * 64, "different-idempotency-key",
                        task["actor_id"], "2026-01-01T00:00:01Z",
                    ),
                )

            recovered = fixture.agent.sync_task(fixture.token, task["task_id"])

            self.assertEqual(recovered["task"]["status"], "blocked")
            self.assertEqual(
                recovered["task"]["error_code"],
                "RECOVERY_RECONCILIATION_REQUIRED",
            )
            with fixture.store.connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM project_versions WHERE parent_version_id=?",
                        (parent_version_id,),
                    ).fetchone()[0],
                    1,
                )

    def test_fact_revision_crash_without_exact_artifact_binding_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp))
            task = self._executing_formal_task(
                fixture,
                "apply_formal_fact_revision",
                {
                    "idempotency_key": "fact-crash-key",
                    "base_version_id": "missing-base-version",
                    "parent_brief_version_id": "missing-parent-brief",
                    "field": "项目名称", "value": "匿名项目",
                    "entrypoint": "assistant", "evidence": "人工补充",
                },
            )

            recovered = fixture.agent.sync_task(fixture.token, task["task_id"])

            self.assertEqual(recovered["task"]["status"], "blocked")
            self.assertEqual(
                recovered["task"]["error_code"],
                "RECOVERY_RECONCILIATION_REQUIRED",
            )

    def test_default_image_adoption_crash_has_no_safe_binding_and_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp))
            task = self._executing_formal_task(
                fixture,
                "adopt_formal_default_images",
                {
                    "idempotency_key": "default-image-crash-key",
                    "base_version_id": "missing-base-version",
                    "selected_image_ids": ["image-1"],
                },
            )

            recovered = fixture.agent.sync_task(fixture.token, task["task_id"])

            self.assertEqual(recovered["task"]["status"], "blocked")
            self.assertEqual(
                recovered["task"]["error_code"],
                "RECOVERY_RECONCILIATION_REQUIRED",
            )

    def test_image_feedback_crash_has_no_task_binding_and_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp))
            task = self._executing_formal_task(
                fixture,
                "record_formal_image_feedback",
                {
                    "idempotency_key": "image-feedback-crash-key",
                    "base_version_id": "missing-base-version",
                    "shot_no": "1", "image_key": "image-1", "reason": "不合适",
                },
            )

            recovered = fixture.agent.sync_task(fixture.token, task["task_id"])

            self.assertEqual(recovered["task"]["status"], "blocked")
            self.assertEqual(
                recovered["task"]["error_code"],
                "RECOVERY_RECONCILIATION_REQUIRED",
            )

    def test_stage_retry_crash_reconciles_exact_recovery_execution_without_second_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp), writes_enabled=False)
            failed = fixture.tasks.create_task(
                fixture.token,
                agent_session_id=fixture.session["agent_session_id"],
                task_type="machine_audit",
                payload_sha256="b" * 64,
                expected_project_version=fixture.project["project_version"],
            )
            fixture.tasks.transition_task(
                fixture.token, task_id=failed["task_id"],
                expected_task_version=failed["task_version"], status="failed",
                details={"stage_id": "machine_audit"}, error_code="PROVIDER_TIMEOUT",
            )
            proposal = fixture.agent.operations.propose_stage_retry(
                fixture.token,
                workspace_id=fixture.workspace_id,
                project_id=fixture.project["project_id"],
                requested_stage_id="machine_audit", reason="崩溃后对账测试",
                expected_project_version=fixture.project["project_version"],
                idempotency_key="retry-crash-key",
            )
            execution = fixture.agent.operations.confirm_stage_retry(
                fixture.token,
                workspace_id=fixture.workspace_id,
                proposal_id=proposal["proposal_id"],
                proposal_digest=proposal["proposal_digest"],
            )
            task = self._executing_formal_task(
                fixture,
                "retry_formal_stage",
                {
                    "idempotency_key": "retry-crash-key",
                    "target_task_id": failed["task_id"],
                    "retry_proposal_id": proposal["proposal_id"],
                    "retry_proposal_digest": proposal["proposal_digest"],
                    "requested_stage_id": "machine_audit",
                },
            )

            recovered = fixture.agent.sync_task(fixture.token, task["task_id"])

            self.assertEqual(recovered["task"]["status"], "completed")
            self.assertEqual(
                recovered["response"]["new_attempt_id"], execution["new_attempt_id"],
            )
            with fixture.store.connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_attempts WHERE task_id=?",
                        (failed["task_id"],),
                    ).fetchone()[0],
                    2,
                )


class FormalAgentContextTests(unittest.TestCase):
    def test_page_owned_requests_return_real_project_links_without_claiming_a_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp))
            for instruction, section in (
                ("保存当前脚本草稿", "script"),
                ("修改 Brief 事实", "brief"),
                ("处理审核退回", "review"),
            ):
                with self.subTest(instruction=instruction):
                    response = fixture.submit(instruction)["response"]
                    self.assertEqual(response["type"], "project_next_step")
                    self.assertEqual(response["next_href"], f"/projects/{fixture.project['project_id']}/{section}")
                    self.assertNotIn("proposal_digest", response)
            self.assertEqual(fixture.projects.list_project_versions(fixture.token, fixture.project["project_id"]), [])

    def test_current_script_image_review_and_brief_are_read_without_a_waiting_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []

            def provider(prompt, *_args):
                context = json.loads(prompt.split("\nCONTEXT:", 1)[1].split("\nUSER:", 1)[0])
                seen.append(context)
                return json.dumps({
                    "intent": "explain_issue", "summary": "请先确认逐镜图片。",
                    "evidence_ids": context["evidence_ids"], "questions": [], "candidate_action": "none",
                })

            fixture = FormalAgentFixture(Path(tmp), advisor=FormalAdvisorService(FormalAdvisorPolicy(
                invoke=provider, provider="anonymous", model="anonymous",
            )))
            reads = []

            def read(kind, model):
                def run(auth_token, **kwargs):
                    self.assertEqual(auth_token, fixture.token)
                    self.assertEqual(kwargs["project_id"], fixture.project["project_id"])
                    self.assertEqual(kwargs["workspace_id"], fixture.workspace_id)
                    reads.append(kind)
                    return model
                return run

            fixture.agent.script_revision = SimpleNamespace(
                brief_adapter=SimpleNamespace(read_current_formal=read("brief", {
                    "confirmation_status": "confirmed", "brief": {"facts": [
                        {"label": "必须出现的内容", "value": "匿名产品演示"},
                    ]},
                })),
                workspace=SimpleNamespace(read_current=read("script", {
                    "state": "ready", "revision": 0, "version_label": "版本 2",
                    "shot_count": 2, "shots": [
                        {"shot_no": "1", "visual": "匿名产品全景。", "voiceover": "查看产品。"},
                        {"shot_no": "2", "visual": "匿名产品细节。", "voiceover": "展示细节。"},
                    ],
                })),
                image_workspace=SimpleNamespace(read_current=read("images", {
                    "state": "temporary_text_basis", "shot_count": 2, "available_image_count": 1,
                    "review_eligible": False, "delivery_eligible": False,
                    "message": "第 2 镜尚需图片。", "shots": [
                        {"shot_no": "1", "confirmed": True, "decision_kind": "image_confirmed"},
                        {"shot_no": "2", "confirmed": True, "decision_kind": "temporary_text_basis", "temporary_reason": "待补细节图"},
                    ],
                })),
            )
            fixture.agent.review_workspace = SimpleNamespace(read_current=read("review", {
                "state": "blocked", "message": "请先确认逐镜图片。", "issues": [],
            }))
            result = fixture.submit("为什么现在不能交付？")
            self.assertEqual(set(reads), {"brief", "script", "images", "review"})
            self.assertIsNone(result["response"]["plan_status"])
            self.assertEqual(seen[0]["script"]["version_label"], "版本 2")
            self.assertEqual(seen[0]["brief"]["facts"][0]["value"], "匿名产品演示")
            self.assertEqual(seen[0]["images"]["shots"][1]["temporary_reason"], "待补细节图")
            self.assertEqual(seen[0]["review"]["message"], "请先确认逐镜图片。")

    def test_recent_dialogue_is_bounded_redacted_and_scoped_to_current_session(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DEEPSEEK_API_KEY": "audit-private-provider-key"}):
            fixture = FormalAgentFixture(Path(tmp), advisor=StaticAdvisor("explain_status"))
            for index in range(8):
                fixture.submit(f"查看状态 {index} audit-private-provider-key")
            context = fixture.agent._context(
                fixture.token, fixture.project, agent_session_id=fixture.session["agent_session_id"],
            )
            history = context["conversation"]
            self.assertTrue(history)
            self.assertLessEqual(len(history), 12)
            serialized = json.dumps(history, ensure_ascii=False)
            self.assertIn("查看状态 7", serialized)
            self.assertNotIn("查看状态 0", serialized)
            self.assertNotIn("audit-private-provider-key", serialized)
            self.assertNotIn(b"audit-private-provider-key", fixture.store.database_path.read_bytes())
            other = fixture.tasks.create_agent_session(fixture.token, project_id=fixture.project["project_id"])
            self.assertEqual(fixture.agent._context(
                fixture.token, fixture.project, agent_session_id=other["agent_session_id"],
            )["conversation"], [])

    def test_empty_candidates_offer_project_materials_and_keep_authoritative_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = FormalAgentFixture(Path(tmp))
            fixture.agent.script_revision = SimpleNamespace(image_workspace=SimpleNamespace(
                read_current=lambda *_a, **_k: {"base_version_id": "version-anonymous"},
                search_shot=lambda *_a, **_k: {
                    "base_version_id": "version-anonymous", "shot_no": "2", "candidates": [],
                    "candidate_snapshot_digest": "a" * 64, "project_version": fixture.project["project_version"],
                },
            ))
            result = fixture.submit("查看第 2 镜图片候选")
            response = result["response"]
            self.assertEqual(response["next_href"], f"/projects/{fixture.project['project_id']}/materials?return_to=shot-images&focus_shot=2")
            self.assertEqual(response["next_label"], "补充图片")
            self.assertEqual(response["candidate_snapshot_digest"], "a" * 64)


class FormalAgentRevisionEntryTests(unittest.TestCase):
    def setUp(self):
        # Reuse the anonymous multi-shot generated project, including real HTTP routes.
        from tests.test_mvp_v2_formal_revision_union import FormalRevisionUnionTests

        self.fixture = FormalRevisionUnionTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.session = self.fixture.tasks.create_agent_session(
            self.fixture.token, project_id=self.fixture.project_id,
        )

    def _submit(self, instruction):
        fixture = self.fixture
        return fixture.client.post(
            f"/api/agent/sessions/{self.session['agent_session_id']}/messages",
            headers=fixture.headers, json={"content": instruction},
        )

    def _seed_redundant_subtitles(self):
        # Empty optional subtitles correctly complete as no_changes. These
        # tests instead need editable targets whose analysis can be resumed.
        fixture = self.fixture
        current = fixture.base.workspace.read_current(
            fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
        )
        for index in (1, 2):
            shot = current["shots"][index]
            current = fixture.revision.apply_table_draft_change(
                fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
                expected_project_version=current["project_version"], expected_revision=current["revision"],
                shot_no=shot["shot_no"], shot_id=shot["shot_id"], field="subtitle",
                value="匿名品牌发布会，呈现匿名品牌发布会的核心宣讲与仪式。",
                idempotency_key=f"agent-subtitle-scope-fixture-{index}",
            )

    def test_scope_and_clarification_can_be_recovered_from_session_and_task(self):
        fixture = self.fixture
        self._seed_redundant_subtitles()
        instruction = "把第 2 镜和第 3 镜的字幕调整得更简洁"
        initial = self._submit(instruction)
        self.assertEqual(initial.status_code, 200, initial.text)
        task_id = initial.json()["response"]["analysis_task_id"]
        expected_href = f"/projects/{fixture.project_id}/script?analysis_task_id={task_id}"
        task = fixture.client.get(f"/api/agent/tasks/{task_id}", headers=fixture.headers)
        self.assertEqual(task.json()["response"]["instruction"], instruction)
        self.assertEqual(task.json()["response"]["next_href"], expected_href)
        session = fixture.client.get(
            f"/api/agent/sessions/{self.session['agent_session_id']}", headers=fixture.headers,
        )
        response = next(item["response"] for item in session.json()["timeline"] if item["task_id"] == task_id)
        self.assertEqual(response["instruction"], instruction)
        self.assertEqual(response["next_href"], expected_href)
        self.assertEqual({item["shot_no"] for item in response["suggested_targets"]}, {"2", "3"})
        clarify = self._submit("把第 2 镜内容删除").json()["response"]
        self.assertEqual(clarify["choices"][0]["instruction"], "删除第 2 镜")
        self.assertIn("[旁白/字幕/备注]", clarify["choices"][1]["instruction"])

    def test_context_reads_real_formal_brief_and_script_after_generation(self):
        fixture = self.fixture
        fixture.agent.generation = fixture.base.generation
        project = fixture.projects.get_project(fixture.token, fixture.project_id)
        context = fixture.agent._context(
            fixture.token, project, agent_session_id=self.session["agent_session_id"],
        )
        self.assertEqual(context["brief"]["confirmation_status"], "confirmed")
        self.assertTrue(context["brief"]["facts"])
        self.assertEqual(len(context["script"]["shots"]), fixture.base.generated.expected_shot_count)
        self.assertIsNone(context["plan"])

    def test_multi_shot_instruction_reaches_existing_canonical_analysis(self):
        fixture = self.fixture
        self._seed_redundant_subtitles()
        before = fixture.base.workspace.read_current(
            fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
        )
        with patch.object(
            fixture.revision, "_propose_assistant_on_task",
            wraps=fixture.revision._propose_assistant_on_task,
        ) as analysis:
            result = self._submit("把第 2 镜和第 3 镜的字幕调整得更简洁")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(analysis.call_count, 1)
        response = result.json()["response"]
        self.assertNotEqual(response.get("error_code"), "INSTRUCTION_AMBIGUOUS")
        self.assertTrue(response.get("analysis_task_id"))
        self.assertEqual({item["shot_no"] for item in response["suggested_targets"]}, {"2", "3"})
        after = fixture.base.workspace.read_current(
            fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
        )
        self.assertEqual(after["shots"], before["shots"])
        self.assertEqual(after["revision"], before["revision"])

    def test_advisor_script_route_proposes_with_project_lock_before_completion(self):
        fixture = self.fixture
        fixture.agent.advisor = StaticAdvisor("propose_script_revision")
        current = fixture.base.workspace.read_current(
            fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
        )
        fixture.revision.apply_table_draft_change(
            fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
            expected_project_version=current["project_version"], expected_revision=current["revision"],
            shot_no="2", shot_id=current["shots"][1]["shot_id"],
            field="voiceover", value="匿名活动旁白待清空。", idempotency_key="agent-clear-fixture",
        )
        before = fixture.projects.list_project_versions(fixture.token, fixture.project_id)
        observed = []
        propose = fixture.revision.propose

        def observe(*args, **kwargs):
            tasks = fixture.tasks.list_agent_session_tasks(
                fixture.token, self.session["agent_session_id"],
            )
            observed.extend(task["status"] for task in tasks if task["task_type"] == "formal_production_advisor")
            return propose(*args, **kwargs)

        with patch.object(fixture.revision, "propose", side_effect=observe):
            result = self._submit("清空第 2 镜旁白")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn("completed", observed)
        self.assertEqual(result.json()["response"]["type"], "action_proposal")
        self.assertEqual(
            fixture.projects.list_project_versions(fixture.token, fixture.project_id), before,
        )

    def test_noop_revision_leaves_no_active_task_and_reports_no_change(self):
        fixture = self.fixture
        fixture.agent.advisor = StaticAdvisor("propose_script_revision")
        before = fixture.base.workspace.read_current(
            fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
        )
        self.assertEqual(before["shots"][1]["voiceover"], "")
        versions = fixture.projects.list_project_versions(fixture.token, fixture.project_id)
        for instruction in ("清空第 2 镜旁白", "把第 2 镜旁白清空"):
            with self.subTest(instruction=instruction):
                self.session = fixture.tasks.create_agent_session(
                    fixture.token, project_id=fixture.project_id,
                )
                result = self._submit(instruction)
                self.assertIn(result.status_code, {200, 409}, result.text)
                payload = result.json()
                response = payload.get("response") or payload.get("detail") or {}
                self.assertRegex(
                    str(response.get("summary") or response.get("message") or ""),
                    "未发生变化|相同|无需修改",
                )
                tasks = fixture.tasks.list_agent_session_tasks(
                    fixture.token, self.session["agent_session_id"],
                )
                active = [
                    {"task_type": item["task_type"], "status": item["status"]}
                    for item in tasks if item["status"] in {
                        "received", "understanding", "executing", "proposed", "waiting_confirmation",
                    }
                ]
                self.assertEqual(active, [], "空修改不能留下仍在处理或等待确认的任务")
                after = fixture.base.workspace.read_current(
                    fixture.token, workspace_id=fixture.workspace_id, project_id=fixture.project_id,
                )
                self.assertEqual(after["shots"], before["shots"])
                self.assertEqual(after["revision"], before["revision"])
                self.assertEqual(
                    fixture.projects.list_project_versions(fixture.token, fixture.project_id), versions,
                )


if __name__ == "__main__":
    unittest.main()
