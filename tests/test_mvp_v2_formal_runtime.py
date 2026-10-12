from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from mvp_v2.formal_console import ProjectPayload
from mvp_v2.formal_runtime import (
    DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS,
    DEEPSEEK_EDITOR_MAX_OUTPUT_TOKENS,
    DEEPSEEK_MODEL,
    DEEPSEEK_PROVIDER,
    DEEPSEEK_WRITER_MAX_OUTPUT_TOKENS,
    FormalCapabilityRouter,
    MINIMAX_VISION_MODEL,
    MINIMAX_VISION_PROVIDER,
    build_formal_runtime,
    build_formal_source_root_resolver,
    uploaded_document,
)
from mvp_v2.local_ui import create_app
from mvp_v2.asset_package import bundle_asset_package_v3, create_asset_package
from mvp_v2.package_registry import LocalPackageRegistry, build_package_record
from tests.test_mvp_v2_asset_package import _write_source


class FormalRuntimeTests(unittest.TestCase):
    def test_image_runtime_uses_zhipu_only_and_never_falls_back_to_seedream(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = {'data_root': str(Path(temporary) / 'data'),
                      'runs_root': str(Path(temporary) / 'runs'), 'source_root': str(Path(temporary) / 'source')}
            with patch.dict(os.environ, {'ZHIPU_API_KEY': '', 'ARK_API_KEY': 'legacy-unused'}):
                self.assertIsNone(build_formal_runtime(config).generation.image_provider)
            with patch.dict(os.environ, {'ZHIPU_API_KEY': 'anonymous-zhipu', 'ARK_API_KEY': 'legacy-unused'}):
                provider = build_formal_runtime(config).generation.image_provider
                self.assertTrue(provider.supports_purpose)
                self.assertTrue(callable(provider.resume))

    def test_formal_project_payload_accepts_six_categories_and_rejects_unknown(self):
        categories = {
            "launch", "exhibition", "annual", "sports", "music_festival", "star_activity",
        }
        self.assertEqual({
            ProjectPayload(name="匿名项目", category_id=value, expected_workspace_version=0).category_id
            for value in categories
        }, categories)
        with self.assertRaises(ValidationError):
            ProjectPayload(name="匿名项目", category_id="unknown", expected_workspace_version=0)

    def test_formal_source_root_uses_configured_category_asset_package(self):
        with tempfile.TemporaryDirectory(prefix="mplus-formal-source-") as temporary:
            root = Path(temporary)
            package_root = root / "package"
            source_root = package_root / "sources"
            source_root.mkdir(parents=True)
            runtime = {"asset_packages": {"明星活动": str(package_root)}}
            with patch("mvp_v2.formal_runtime.asset_package_source_record") as validate, patch(
                "mvp_v2.formal_runtime.load_asset_package",
                return_value={"source_root_path": source_root},
            ):
                resolver = build_formal_source_root_resolver(runtime, root / "fallback")
                self.assertEqual(resolver("明星活动"), source_root.resolve())
            validate.assert_called_once_with(str(package_root), expected_category_id="star_activity")
            self.assertEqual(
                build_formal_source_root_resolver({}, root / "fallback")("发布会"),
                (root / "fallback").resolve(),
            )

    def test_formal_resolver_uses_registry_alias_over_direct_package_config(self):
        with tempfile.TemporaryDirectory(prefix="mplus-formal-registry-") as temporary:
            root = Path(temporary)
            source_v4 = root / "source-v4"
            source_v5 = root / "source-v5"
            source_v4.mkdir()
            source_v5.mkdir()
            _write_source(source_v4 / "M+发布会类通用脚本库.xlsx")
            _write_source(source_v5 / "M+发布会类通用脚本库.xlsx")
            editable_v4 = create_asset_package(root / "editable-v4", package_id="launch-core", source_root=source_v4)
            editable_v5 = create_asset_package(root / "editable-v5", package_id="launch-core", source_root=source_v5)
            v4 = bundle_asset_package_v3(editable_v4, root / "packages" / "v4", package_version=4)
            v5 = bundle_asset_package_v3(editable_v5, root / "packages" / "v5", package_version=5)
            registry = LocalPackageRegistry(root / "registry.json")
            registry.register_records([build_package_record(v4)], promote_formal=True)
            registry.register_records([build_package_record(v5)], promote_formal=True)

            runtime = {
                "package_registry_path": str(root / "registry.json"),
                "asset_packages": {"发布会": str(v4)},
            }
            resolved = build_formal_source_root_resolver(runtime, root / "fallback")("发布会")

            self.assertEqual(resolved, (root / "packages" / "v5" / "sources").resolve())
            self.assertNotEqual(resolved, (root / "packages" / "v4" / "sources").resolve())

    def test_formal_resolver_keeps_direct_compatibility_when_registry_file_is_absent(self):
        with tempfile.TemporaryDirectory(prefix="mplus-formal-registry-missing-") as temporary:
            root = Path(temporary)
            package_root = root / "package"
            source_root = package_root / "sources"
            source_root.mkdir(parents=True)
            runtime = {
                "package_registry_path": str(root / "missing-registry.json"),
                "asset_packages": {"明星活动": str(package_root)},
            }
            with patch("mvp_v2.formal_runtime.asset_package_source_record") as validate, patch(
                "mvp_v2.formal_runtime.load_asset_package",
                return_value={"source_root_path": source_root},
            ):
                resolver = build_formal_source_root_resolver(runtime, root / "fallback")
                self.assertEqual(resolver("明星活动"), source_root.resolve())
            validate.assert_called_once_with(str(package_root), expected_category_id="star_activity")
    def test_runtime_wires_one_database_and_server_owned_deepseek_route(self):
        with tempfile.TemporaryDirectory(prefix="mplus-formal-runtime-") as temporary:
            root = Path(temporary)
            config = {
                "data_root": str(root / "data"),
                "runs_root": str(root / "data" / "runs"),
                "source_root": str(root / "source"),
            }
            calls = []

            def provider(prompt, model, tokens):
                calls.append((prompt, model, tokens))
                return "{}"

            with patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "anonymous-test-key",
                "MPLUS_FORMAL_AGENT_WRITES": "0",
            }), patch(
                "mvp_v2.formal_runtime.build_text_provider", return_value=provider,
            ) as factory, patch(
                "mvp_v2.formal_runtime.provider_credentials_available",
                side_effect=lambda provider_name: provider_name in {
                    DEEPSEEK_PROVIDER, MINIMAX_VISION_PROVIDER,
                },
            ):
                bundle = build_formal_runtime(config)
            factory.assert_called_once_with(DEEPSEEK_PROVIDER)
            policy = bundle.generation.provider_policy
            self.assertIsNotNone(policy)
            self.assertEqual(
                (
                    policy.writer_provider, policy.writer_model,
                    policy.editor_provider, policy.editor_model,
                ),
                (DEEPSEEK_PROVIDER, DEEPSEEK_MODEL, DEEPSEEK_PROVIDER, DEEPSEEK_MODEL),
            )
            self.assertIs(policy.writer, policy.editor)
            self.assertIsNotNone(bundle.brief_adapter.understanding_policy)
            self.assertEqual(bundle.brief_adapter.understanding_policy.model, DEEPSEEK_MODEL)
            self.assertIsNotNone(bundle.generation.vision_provider_policy)
            self.assertEqual(
                (
                    bundle.generation.vision_provider_policy.provider,
                    bundle.generation.vision_provider_policy.model,
                ),
                (MINIMAX_VISION_PROVIDER, MINIMAX_VISION_MODEL),
            )
            self.assertEqual(calls, [], "运行时启动不得调用真实文本模型")
            self.assertIsNotNone(bundle.agent.advisor)
            self.assertEqual(bundle.agent.advisor.policy.provider, DEEPSEEK_PROVIDER)
            self.assertEqual(bundle.agent.advisor.policy.model, DEEPSEEK_MODEL)
            self.assertEqual(
                bundle.agent.advisor.policy.max_output_tokens,
                DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS,
            )
            self.assertEqual(bundle.agent.setup_status()["formal_agent_writes"], "available")
            self.assertEqual(
                bundle.agent.advisor.policy.invoke(
                    "{}", DEEPSEEK_MODEL, DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS,
                ),
                "{}",
            )
            self.assertEqual(calls[-1][2], DEEPSEEK_AGENT_MAX_OUTPUT_TOKENS)
            self.assertEqual(policy.writer("{}", DEEPSEEK_MODEL, 65536), "{}")
            self.assertEqual(calls[-1][2], DEEPSEEK_WRITER_MAX_OUTPUT_TOKENS)
            self.assertEqual(
                policy.editor("{}\nDRAFT:{}", DEEPSEEK_MODEL, 65536), "{}",
            )
            self.assertEqual(calls[-1][2], DEEPSEEK_EDITOR_MAX_OUTPUT_TOKENS)
            database = bundle.agent.services.store.database_path
            self.assertEqual(bundle.brief_adapter.service.store.database_path, database)
            self.assertEqual(bundle.script_workspace.services.store.database_path, database)
            self.assertEqual(bundle.image_workspace.services.store.database_path, database)
            self.assertIs(
                bundle.script_workspace.materializer.source_root_resolver,
                bundle.generation.source_root_resolver,
            )
            self.assertIs(
                bundle.image_workspace.materializer.source_root_resolver,
                bundle.generation.source_root_resolver,
            )
            client = TestClient(create_app(
                agent_api=bundle.agent,
                formal_brief_adapter=bundle.brief_adapter,
                formal_brief_upload_factory=uploaded_document,
                formal_generation_service=bundle.generation,
                formal_script_service=bundle.script_workspace,
                formal_image_service=bundle.image_workspace,
            ))
            self.addCleanup(client.close)
            setup = client.get("/api/formal/setup")
            self.assertEqual(setup.status_code, 200)
            self.assertEqual(setup.json()["brief_adapter"], "ready")
            self.assertEqual(setup.json()["project_capability"], "workspace_required")
            self.assertNotIn("production_certification", setup.json())

    def test_server_owned_capability_router_assigns_text_and_vision_roles(self):
        with patch(
            "mvp_v2.formal_runtime.provider_credentials_available",
            side_effect=lambda provider: provider in {DEEPSEEK_PROVIDER, MINIMAX_VISION_PROVIDER},
        ):
            router = FormalCapabilityRouter()
        for capability in (
            "brief_text_understanding", "script_writer", "script_editor",
            "production_advisor",
        ):
            route = router.resolve(capability)
            self.assertEqual((route.provider, route.model, route.available), (DEEPSEEK_PROVIDER, DEEPSEEK_MODEL, True))
        vision = router.resolve("image_understanding")
        self.assertEqual((vision.provider, vision.model, vision.available), (MINIMAX_VISION_PROVIDER, MINIMAX_VISION_MODEL, True))
        with self.assertRaisesRegex(ValueError, "未声明"):
            router.resolve("browser_selected_model")

    def test_formal_agent_readiness_is_per_action_and_old_write_switch_is_ignored(self):
        with tempfile.TemporaryDirectory(prefix="mplus-formal-agent-runtime-") as temporary:
            root = Path(temporary)
            config = {
                "data_root": str(root / "data"),
                "runs_root": str(root / "data" / "runs"),
                "source_root": str(root / "source"),
            }
            with patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "runtime-only-test-key",
                "MPLUS_FORMAL_AGENT_WRITES": "",
            }), patch(
                "mvp_v2.formal_runtime.build_text_provider",
                return_value=lambda *_args: "{}",
            ), patch(
                "mvp_v2.formal_runtime.provider_credentials_available",
                side_effect=lambda provider: provider == DEEPSEEK_PROVIDER,
            ):
                enabled = build_formal_runtime(config)
            self.assertEqual(enabled.agent.setup_status()["formal_agent_writes"], "available")
            self.assertEqual(
                enabled.agent.setup_status()["formal_actions"]["prepare_formal_plan"]["state"],
                "available",
            )
            self.assertEqual(
                enabled.agent.setup_status()["formal_actions"]["confirm_formal_plan_generation"]["state"],
                "available",
            )

            with patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "",
                "MPLUS_FORMAL_AGENT_WRITES": "",
            }), patch(
                "mvp_v2.formal_runtime.provider_credentials_available",
                return_value=False,
            ):
                unavailable = build_formal_runtime({
                    **config,
                    "data_root": str(root / "data-unavailable"),
                    "runs_root": str(root / "data-unavailable" / "runs"),
                })
            status = unavailable.agent.setup_status()
            self.assertEqual(status["formal_advisor"], "blocked")
            self.assertEqual(status["formal_agent_writes"], "available")
            self.assertEqual(
                status["formal_actions"]["prepare_formal_plan"]["state"],
                "available",
            )
            self.assertEqual(
                status["formal_actions"]["confirm_formal_plan_generation"]["state"],
                "blocked",
            )
            self.assertEqual(
                status["formal_actions"]["confirm_formal_plan_generation"]["reason"],
                "脚本生成所需的受控文本 Provider 未就绪",
            )

            with patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "runtime-only-test-key",
                "MPLUS_FORMAL_AGENT_READ_ONLY": "1",
                "MPLUS_FORMAL_AGENT_WRITES": "1",
            }), patch(
                "mvp_v2.formal_runtime.build_text_provider",
                return_value=lambda *_args: "{}",
            ), patch(
                "mvp_v2.formal_runtime.provider_credentials_available",
                return_value=True,
            ):
                readonly = build_formal_runtime({
                    **config,
                    "data_root": str(root / "data-readonly"),
                    "runs_root": str(root / "data-readonly" / "runs"),
                })
            self.assertEqual(
                readonly.agent.setup_status()["formal_actions"]["prepare_formal_plan"]["state"],
                "blocked",
            )
            self.assertEqual(
                readonly.agent.setup_status()["formal_actions"]["prepare_formal_plan"]["reason"],
                "当前部署已显式设为只读",
            )


if __name__ == "__main__":
    unittest.main()
