import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from mvp_v2.agent_tools import (
    TOOL_REGISTRY,
    ToolError,
    ToolExecutionResult,
    export_tool_schemas,
    validate_tool_input,
)
from mvp_v2.manual_actions import MANUAL_ACTION_REGISTRY, validate_manual_action_input


class AgentToolContractTests(unittest.TestCase):
    def test_registry_exposes_every_planned_tool_with_stable_metadata(self):
        expected = {
            "get_project_state",
            "get_run_details",
            "explain_blocker",
            "preflight_brief",
            "get_formal_project_context",
            "get_requirement_coverage",
            "explain_formal_issue",
            "prepare_formal_plan",
            "confirm_formal_plan_generation",
            "apply_formal_script_revision",
            "apply_formal_image_revision",
            "record_formal_image_feedback",
            "apply_formal_fact_revision",
            "retry_formal_stage",
            "adopt_formal_default_images",
            "create_project",
            "start_generation",
            "retry_stage",
            "fork_revision",
            "cancel_task",
            "compare_versions",
            "get_creative_strategies",
            "select_creative_strategy",
            "search_assets",
            "create_asset_import",
            "review_asset_import",
            "get_review_readiness",
            "prepare_human_review",
            "create_knowledge_candidate",
        }

        self.assertEqual(set(TOOL_REGISTRY), expected)
        for name, definition in TOOL_REGISTRY.items():
            self.assertIn(definition.risk, {"R0", "R1", "R2"})
            self.assertTrue(definition.permission)
            self.assertGreater(definition.timeout_seconds, 0)
            self.assertTrue(definition.audit_event.startswith("agent_tool."))
            self.assertEqual(definition.name, name)

    def test_r3_publication_and_approval_actions_are_manual_page_only(self):
        forbidden_agent_actions = {
            "publish_version",
            "publish_asset_import",
            "approve_knowledge",
            "record_version_review",
            "select_candidate_version",
            "delete_asset",
            "force_recover",
            "archive_project",
            "restore_project",
        }
        self.assertTrue(forbidden_agent_actions.isdisjoint(TOOL_REGISTRY))
        self.assertEqual(
            set(MANUAL_ACTION_REGISTRY),
            {
                "record_version_review",
                "select_candidate_version",
                "publish_version",
                "publish_asset_import",
                "approve_knowledge",
                "archive_project",
                "restore_project",
            },
        )
        for name in MANUAL_ACTION_REGISTRY:
            with self.assertRaises(KeyError):
                validate_tool_input(name, {})
        root = Path(__file__).parents[1] / "schemas" / "agent_tools"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        for name in forbidden_agent_actions - {"delete_asset", "force_recover", "archive_project", "restore_project"}:
            self.assertNotIn(name, manifest["tools"])
            self.assertFalse((root / f"{name}.input.schema.json").exists())
            self.assertFalse((root / f"{name}.output.schema.json").exists())
        with self.assertRaises(ValidationError):
            validate_manual_action_input(
                "publish_version",
                {
                    "project_id": "project_12345678",
                    "version_id": "version_12345678",
                    "review_id": "review_12345678",
                    "expected_project_version": 1,
                    "idempotency_key": "publication-00000001",
                },
            )
        with self.assertRaises(ValidationError):
            validate_manual_action_input(
                "approve_knowledge",
                {
                    "candidate_id": "candidate_12345678",
                    "level": "personal",
                    "review_evidence": "人工复核",
                    "expected_knowledge_version": 0,
                    "idempotency_key": "approval-00000001",
                    "confirmation_token": "confirmation-placeholder-00000000",
                },
            )

    def test_write_tool_requires_idempotency_and_matching_version_token(self):
        valid = validate_tool_input(
            "start_generation",
            {
                "project_id": "project_12345678",
                "mode": "standard",
                "expected_project_version": 3,
                "idempotency_key": "request-00000001",
            },
        )

        self.assertEqual(valid.expected_project_version, 3)
        for missing in ("idempotency_key", "expected_project_version"):
            payload = {
                "project_id": "project_12345678",
                "mode": "standard",
                "expected_project_version": 3,
                "idempotency_key": "request-00000001",
            }
            payload.pop(missing)
            with self.assertRaises(ValidationError):
                validate_tool_input("start_generation", payload)

        for definition in TOOL_REGISTRY.values():
            if definition.risk not in {"R2", "R3"}:
                continue
            properties = definition.input_model.model_json_schema()["properties"]
            self.assertIn("idempotency_key", properties, definition.name)
            self.assertTrue(
                {
                    "expected_workspace_version",
                    "expected_project_version",
                    "expected_active_package_version",
                    "expected_knowledge_version",
                }
                & set(properties),
                definition.name,
            )

    def test_review_readiness_is_read_only_but_package_creation_is_r2(self):
        self.assertEqual(TOOL_REGISTRY["get_review_readiness"].risk, "R1")
        self.assertEqual(TOOL_REGISTRY["prepare_human_review"].risk, "R2")
        self.assertNotIn("record_version_review", TOOL_REGISTRY)
        with self.assertRaises(ValidationError):
            validate_tool_input(
                "prepare_human_review",
                {"project_id": "project_12345678", "run_id": "run_12345678"},
            )

    def test_formal_tools_are_server_owned_and_require_two_r2_confirmations(self):
        for name in (
            "get_formal_project_context", "get_requirement_coverage", "explain_formal_issue",
        ):
            self.assertEqual(TOOL_REGISTRY[name].risk, "R0")
            self.assertFalse(TOOL_REGISTRY[name].confirmation_required)
        for name in ("prepare_formal_plan", "confirm_formal_plan_generation"):
            definition = TOOL_REGISTRY[name]
            self.assertEqual(definition.risk, "R2")
            self.assertEqual(definition.idempotency_scope, "project")
            self.assertTrue(definition.confirmation_required)
        with self.assertRaises(ValidationError):
            validate_tool_input("prepare_formal_plan", {
                "project_id": "project_12345678",
                "expected_project_version": 1,
                "idempotency_key": "formal-plan-00000001",
                "model": "employee-selected-model",
            })
        accepted = validate_tool_input("prepare_formal_plan", {
            "project_id": "project_12345678",
            "expected_project_version": 1,
            "idempotency_key": "formal-plan-00000001",
            "generation_mode": "text_only_draft",
        })
        self.assertEqual(accepted.generation_mode, "text_only_draft")
        with self.assertRaises(ValidationError):
            validate_tool_input("confirm_formal_plan_generation", {
                "project_id": "project_12345678",
                "plan_id": "task_formalplan12345678",
                "plan_sha256": "a" * 64,
                "expected_project_version": 1,
                "idempotency_key": "formal-generation-00000001",
                "path": "/tmp/forged",
            })

    def test_tool_input_rejects_actor_spoofing_and_unknown_fields(self):
        with self.assertRaises(ValidationError):
            validate_tool_input(
                "get_project_state",
                {"project_id": "project_12345678", "actor_id": "owner"},
            )

    def test_fork_revision_rejects_client_supplied_delivery_context(self):
        payload = {
            "project_id": "project_12345678",
            "expected_project_version": 3,
            "idempotency_key": "revision-00000001",
            "parent_run_id": "run_parent_12345678",
            "modification_plan": {"shot_no": "1", "target_field": "画面描述"},
            "delivery_context": {"version_id": "forged"},
        }

        with self.assertRaises(ValidationError):
            validate_tool_input("fork_revision", payload)

    def test_preflight_requires_exactly_one_input_source(self):
        validate_tool_input(
            "preflight_brief",
            {"brief": {"项目名称": "匿名项目", "脚本类型": "发布会"}},
        )
        validate_tool_input("preflight_brief", {"project_id": "project_12345678"})

        with self.assertRaises(ValidationError):
            validate_tool_input("preflight_brief", {})
        with self.assertRaises(ValidationError):
            validate_tool_input(
                "preflight_brief",
                {
                    "project_id": "project_12345678",
                    "brief": {"项目名称": "匿名项目"},
                },
            )

    def test_category_knowledge_target_accepts_real_category_id(self):
        candidate = validate_tool_input(
            "create_knowledge_candidate",
            {
                "scope": "category",
                "target_id": "launch",
                "statement": "匿名类别经验候选",
                "evidence_refs": ["artifact://knowledge/evidence/anonymous"],
                "expected_knowledge_version": 0,
                "idempotency_key": "knowledge-00000001",
            },
        )

        self.assertEqual(candidate.target_id, "launch")

    def test_result_envelope_uses_logical_artifact_refs_and_stable_errors(self):
        result = ToolExecutionResult(
            task_id="task_12345678",
            status="blocked",
            artifact_refs=["artifact://run/run_123/revision_report"],
            warnings=["等待人工复核"],
            error=ToolError(
                code="REVIEW_REQUIRED",
                message="人工审核尚未通过",
                retryable=False,
            ),
        )

        self.assertEqual(result.status, "blocked")
        with self.assertRaises(ValidationError):
            ToolExecutionResult(
                task_id="task_12345678",
                status="completed",
                artifact_refs=["/tmp/secret.xlsx"],
            )

    def test_partial_default_image_adoption_accepts_declared_pending_shot_count(self):
        value = validate_tool_input("adopt_formal_default_images", {
            "project_id": "project_12345678",
            "expected_project_version": 3,
            "idempotency_key": "default-images-12345678",
            "package_id": "package_12345678",
            "package_version": 2,
            "package_manifest_sha256": "a" * 64,
            "selections": [{
                "shot_no": "1", "media_id": "media_12345678",
            }],
            "pending_shot_count": 1,
            "allowed_shot_ids": ["shot_12345678"],
        })

        self.assertEqual(value.pending_shot_count, 1)
        self.assertEqual(value.allowed_shot_ids, ["shot_12345678"])

    def test_schema_export_is_deterministic_and_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            written = export_tool_schemas(root)

            self.assertEqual(len(written), len(TOOL_REGISTRY) * 2 + 1)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["tools"]), set(TOOL_REGISTRY))
            for path in written:
                self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)
            committed = Path(__file__).parents[1] / "schemas" / "agent_tools"
            self.assertEqual(
                {path.name: path.read_text(encoding="utf-8") for path in written},
                {
                    path.name: path.read_text(encoding="utf-8")
                    for path in committed.glob("*.json")
                },
            )


if __name__ == "__main__":
    unittest.main()
