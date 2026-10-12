import signal
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from openpyxl import Workbook

from mvp_v2.local_ui import (
    ArtifactRegistry,
    BRIEF_PRESETS,
    BriefForm,
    Job,
    JobManager,
    LaunchOptions,
    LaunchPayload,
    classify_report,
    create_app,
    existing_ui_running,
    export_waiting_review_package,
    _failure_message,
    launch_payload_from_run,
    load_ui_network_settings,
    main as local_ui_main,
    preflight_payload,
    register_report_artifacts,
    read_stage_progress,
    resolve_model_route,
    scan_history,
    stage_progress_summary,
    model_status_summary,
    portable_visual_review_path,
    validated_formal_agent_header,
)
from mvp_v2.stage_contract import STAGE_ORDER
from mvp_v2.preflight import CheckResult, PreflightReport, run_launch_preflight
from mvp_v2.workspace_store import WorkspaceServiceError


def _write_source(path: Path) -> None:
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    template.cell(5, 1, "结构")
    template.cell(5, 2, "镜号")
    for col, value in enumerate(["画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注"], 3):
        template.cell(6, col, value)
    for row in range(7, 25):
        template.cell(row, 1, "开场造势")
        template.cell(row, 2, row - 6)
        template.cell(row, 9, f"测试镜头{row - 6}")
    creative = workbook.create_sheet("创意库")
    creative.cell(5, 1, "镜号")
    creative.cell(5, 2, "结构")
    for col, value in enumerate(["画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"], 4):
        creative.cell(6, col, value)
    creative.cell(7, 1, "C001")
    creative.cell(7, 2, "开场造势")
    creative.cell(7, 9, "测试创意")
    ai_spec = workbook.create_sheet("AI_规范表")
    ai_spec.cell(1, 1, "测试 fixture：真实运行仍由 SourceCatalog 校验完整 AI 规范数据")
    workbook.save(path)


def _brief(material: Path, duration: str = "60s") -> dict:
    return {
        "项目名称": "测试发布会",
        "项目区域文件名": "测试发布会.xlsx",
        "脚本类型": "发布会",
        "品牌或客户": "测试品牌",
        "行业或活动类别": "科技发布会",
        "视频时长": duration,
        "风格调性": "冷峻科技感",
        "必须出现的内容": "品牌logo、主会场",
        "禁忌或不能出现的内容": "竞品",
        "素材目录或素材文件": str(material),
        "备注": "测试",
    }


class UiNetworkSettingsTests(unittest.TestCase):
    def test_github_agent_bypass_requires_valid_session_and_never_allows_owner_bootstrap(self):
        class Identity:
            def validate_session(self, token):
                if token != "valid-agent-session":
                    raise WorkspaceServiceError("AUTH_SESSION_INVALID", "invalid")
                return {"actor_id": "actor-anonymous"}

        api = SimpleNamespace(services=SimpleNamespace(identity=Identity()))
        self.assertTrue(validated_formal_agent_header(
            api, "/api/formal/workspaces/ws/projects", "valid-agent-session",
        ))
        self.assertFalse(validated_formal_agent_header(
            api, "/api/formal/workspaces/ws/projects", "forged-header",
        ))
        self.assertFalse(validated_formal_agent_header(
            api, "/api/formal/owner", "valid-agent-session",
        ))

    def test_local_defaults_remain_loopback_only(self):
        settings = load_ui_network_settings({})

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8765)
        self.assertEqual(settings.public_url, "http://127.0.0.1:8765")
        self.assertEqual(
            settings.allowed_hosts,
            ("127.0.0.1", "localhost", "testserver"),
        )

    def test_dev_profile_uses_isolated_default_port(self):
        settings = load_ui_network_settings({"MPLUS_PROFILE": "dev"})

        self.assertEqual(settings.port, 8766)
        self.assertEqual(settings.public_url, "http://127.0.0.1:8766")

    def test_external_bind_requires_explicit_hosts_origins_and_public_url(self):
        with self.assertRaisesRegex(ValueError, "非本机监听"):
            load_ui_network_settings({"MPLUS_HOST": "0.0.0.0"})

        settings = load_ui_network_settings({
            "MPLUS_HOST": "0.0.0.0",
            "MPLUS_PORT": "8080",
            "MPLUS_ALLOWED_HOSTS": "staging.mplus.example.com,127.0.0.1",
            "MPLUS_ALLOWED_ORIGINS": "https://staging.mplus.example.com",
            "MPLUS_PUBLIC_URL": "https://staging.mplus.example.com",
            "MPLUS_AUTH_MODE": "github",
            "MPLUS_GITHUB_CLIENT_ID": "client-id",
            "MPLUS_GITHUB_CLIENT_SECRET": "client-secret",
            "MPLUS_GITHUB_ALLOW_USERS": "approved-user",
            "MPLUS_GITHUB_SESSION_SECRET": "s" * 32,
            "MPLUS_POSTGRES_DSN": "postgresql://example.invalid/mplus",
        })

        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 8080)
        self.assertEqual(
            settings.allowed_hosts,
            ("staging.mplus.example.com", "127.0.0.1"),
        )
        self.assertEqual(
            settings.allowed_origins,
            frozenset({"https://staging.mplus.example.com"}),
        )

    def test_external_network_configuration_rejects_wildcards_paths_and_bad_ports(self):
        valid = {
            "MPLUS_HOST": "0.0.0.0",
            "MPLUS_ALLOWED_HOSTS": "staging.mplus.example.com",
            "MPLUS_ALLOWED_ORIGINS": "https://staging.mplus.example.com",
            "MPLUS_PUBLIC_URL": "https://staging.mplus.example.com",
            "MPLUS_AUTH_MODE": "github",
            "MPLUS_GITHUB_CLIENT_ID": "client-id",
            "MPLUS_GITHUB_CLIENT_SECRET": "client-secret",
            "MPLUS_GITHUB_ALLOW_USERS": "approved-user",
            "MPLUS_GITHUB_SESSION_SECRET": "s" * 32,
            "MPLUS_POSTGRES_DSN": "postgresql://example.invalid/mplus",
        }
        for update in (
            {"MPLUS_PORT": "0"},
            {"MPLUS_PORT": "not-a-port"},
            {"MPLUS_ALLOWED_HOSTS": "*"},
            {"MPLUS_ALLOWED_HOSTS": "https://staging.mplus.example.com"},
            {"MPLUS_ALLOWED_ORIGINS": "https://staging.mplus.example.com/path"},
            {"MPLUS_PUBLIC_URL": "https://other.example.com"},
        ):
            with self.subTest(update=update):
                with self.assertRaises(ValueError):
                    load_ui_network_settings({**valid, **update})


def _payload(root: Path) -> LaunchPayload:
    return LaunchPayload(
        brief=BriefForm(
            project_name="测试发布会",
            client="测试品牌",
            industry="科技发布会",
            tone="冷峻科技感",
            required_content="品牌logo、主会场",
            forbidden_content="竞品",
            material_path=str(root / "media"),
        ),
        options=LaunchOptions(source_root=str(root / "source"), output_dir=str(root / "out")),
    )


class ModelRouteTests(unittest.TestCase):
    def test_adaptive_uses_deepseek_writer_and_minimax_m3_editor(self):
        options = LaunchOptions(model_profile="adaptive")

        writer_provider, writer_model, editor_provider, editor_model, vision_model = (
            resolve_model_route(options)
        )

        self.assertEqual((writer_provider, writer_model), ("deepseek-http", "deepseek-v4-pro"))
        self.assertEqual((editor_provider, editor_model), ("minimax-cli", "MiniMax-M3"))
        self.assertEqual(vision_model, "")

    def test_minimax_m3_profile_uses_m3_for_both_text_passes(self):
        route = resolve_model_route(LaunchOptions(model_profile="minimax-m3"))

        self.assertEqual(
            route,
            ("minimax-cli", "MiniMax-M3", "minimax-cli", "MiniMax-M3", ""),
        )

    def test_deepseek_only_profile_keeps_both_text_passes_on_deepseek(self):
        route = resolve_model_route(LaunchOptions(model_profile="deepseek-only"))

        self.assertEqual(
            route,
            (
                "deepseek-http",
                "deepseek-v4-pro",
                "deepseek-http",
                "deepseek-v4-pro",
                "",
            ),
        )

    def test_parent_run_restore_preserves_exact_hybrid_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "input_freeze.json").write_text(
                json.dumps(
                    {
                        "brief": _brief(Path(tmp)),
                        "config": {
                            "quality_profile": "client-review",
                            "writer_provider": "deepseek-http",
                            "writer_model": "deepseek-v4-pro",
                            "editor_provider": "minimax-cli",
                            "editor_model": "MiniMax-M3",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            payload = launch_payload_from_run(run_dir)

        self.assertEqual(payload.options.model_profile, "adaptive")
        self.assertEqual(
            resolve_model_route(payload.options),
            ("deepseek-http", "deepseek-v4-pro", "minimax-cli", "MiniMax-M3", ""),
        )

    def test_failure_message_surfaces_the_actionable_child_run_error(self):
        message = _failure_message([
            "Traceback (most recent call last):",
            "ValueError: 父运行 Brief 已变化，不能复用上游节点",
            "检查结论：可以开始运行",
        ])

        self.assertEqual(
            message,
            "运行失败：父运行 Brief 已变化，不能复用上游节点",
        )

    def test_draft_status_does_not_claim_external_text_models_ran(self):
        rows = model_status_summary(
            LaunchOptions(quality_profile="draft", model_profile="dual-ab"),
            [{"stage_id": "script_draft", "status": "succeeded"}],
            None,
        )

        self.assertEqual([item["role"] for item in rows], ["视觉理解", "文本草稿"])
        self.assertEqual(rows[1]["provider"], "本地规则")
        self.assertEqual(rows[1]["model"], "未调用外部文本模型")

    def test_existing_ui_health_check_bypasses_system_proxy(self):
        response = Mock()
        response.read.return_value = b'{"service":"mplus-launch-ui"}'
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = context

        with patch("mvp_v2.local_ui.build_opener", return_value=opener):
            self.assertTrue(existing_ui_running())

        opener.open.assert_called_once_with(
            "http://127.0.0.1:8765/api/health",
            timeout=0.8,
        )

    def test_dual_ab_uses_provider_managed_vision_instead_of_fake_m3_model(self):
        options = LaunchOptions(model_profile="dual-ab")

        writer_provider, writer_model, editor_provider, editor_model, vision_model = (
            resolve_model_route(options)
        )

        self.assertEqual((writer_provider, writer_model), ("deepseek-http", "deepseek-v4-pro"))
        self.assertEqual(
            (editor_provider, editor_model),
            ("minimax-cli", "MiniMax-M2.7-highspeed"),
        )
        self.assertEqual(vision_model, "")


class StructuredPreflightTests(unittest.TestCase):
    def test_structured_brief_passes_the_same_contract_as_file_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            media = root / "media"
            media.mkdir()
            (media / "01.png").write_bytes(b"image")
            with patch("mvp_v2.preflight.minimax_credentials_available", return_value=True):
                report = run_launch_preflight(
                    brief_data=_brief(media),
                    source_root=source,
                    project_dir=media,
                    output_dir=root / "out",
                    asset_cache_root=root / "cache",
                )
            self.assertTrue(report.passed, report.render())

    def test_structured_brief_rejects_59_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_launch_preflight(
                brief_data=_brief(root, "59s"),
                source_root=root / "source",
                project_dir=root / "media",
                output_dir=root / "out",
                asset_cache_root=root / "cache",
            )
            self.assertFalse(report.passed)
            self.assertTrue(any("60" in check.message for check in report.checks if check.status == "fail"))

    def test_preflight_warns_visual_scope_that_would_create_hundreds_of_ai_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            media = root / "media"
            media.mkdir()
            for index in range(16):
                (media / f"video-{index:02d}.mp4").write_bytes(b"video")
            with patch("mvp_v2.preflight.minimax_credentials_available", return_value=True):
                report = run_launch_preflight(
                    brief_data=_brief(media),
                    source_root=source,
                    project_dir=media,
                    output_dir=root / "out",
                    asset_cache_root=root / "cache",
                )
            self.assertTrue(report.passed)
            self.assertTrue(any(check.name == "项目素材规模" and check.status == "warn" for check in report.checks))
            self.assertTrue(any("192" in check.message for check in report.checks))


class LocalUiApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = TestClient(self.app)
        self.bootstrap = self.client.get("/api/bootstrap").json()
        self.headers = {"X-MPlus-UI-Token": self.bootstrap["token"]}

    def test_product_assets_follow_build_when_browser_keeps_previous_assets_cached(self):
        import re
        from mvp_v2.local_ui import UI_ROOT

        original = Path.read_bytes
        first = self.client.get("/projects")
        first_build = re.search(r'name="mplus-ui-build" content="([^"]+)"', first.text).group(1)

        def updated_asset(path):
            content = original(path)
            return content + b"\n/* changed build */" if path == UI_ROOT / "app.js" else content

        with patch.object(Path, "read_bytes", updated_asset):
            second = self.client.get("/projects")
        second_build = re.search(r'name="mplus-ui-build" content="([^"]+)"', second.text).group(1)
        self.assertNotEqual(first_build, second_build)
        self.assertEqual(second.headers["cache-control"], "no-cache")
        for asset in ("app.js", "app.css", "image-creation.js"):
            self.assertIn(f'/assets/{asset}?v={first_build}', first.text)
            self.assertIn(f'/assets/{asset}?v={second_build}', second.text)

    def test_bootstrap_exposes_local_defaults_and_token(self):
        self.assertTrue(self.bootstrap["token"])
        self.assertIn("source_root", self.bootstrap["defaults"])
        self.assertEqual(self.bootstrap["defaults"]["quality_profile"], "client-review")
        self.assertGreaterEqual(len(self.bootstrap["system_checks"]), 4)
        self.assertEqual(set(self.bootstrap["brief_presets"]), set(BRIEF_PRESETS))
        for script_type, preset in self.bootstrap["brief_presets"].items():
            brief = BriefForm(**preset)
            self.assertEqual(brief.script_type, script_type)
            self.assertTrue(brief.project_name)
            self.assertTrue(brief.tone)
            self.assertTrue(brief.required_content)

    def test_diagnose_reports_failures_without_starting_server(self):
        checks = [
            {"name": "本地 Python", "status": "pass", "message": "3.11"},
            {"name": "明星活动资料库", "status": "fail", "message": "文件不存在"},
        ]
        with patch("mvp_v2.local_ui.system_checks", return_value=checks), patch(
            "mvp_v2.local_ui.existing_ui_running"
        ) as existing:
            result = local_ui_main(["--diagnose", "--no-open"])

        self.assertEqual(result, 2)
        existing.assert_not_called()

    def test_non_launch_presets_use_source_backed_category_requirements(self):
        expected = {
            "展会": {"品牌展台", "入口", "核心产品", "观众互动", "品牌落版"},
            "年会": {"企业 Logo", "员工入场", "高管致辞", "团队颁奖", "全员合影"},
            "赛事": {"赛事标识", "邀请函入场", "起跑集结", "竞技瞬间", "冲线或颁奖"},
            "音乐节": {"活动主视觉", "活动场地", "舞台表演", "观众互动", "品牌落版"},
            "明星活动": {"品牌 Logo", "活动空间", "明星出场", "产品互动", "品牌落版"},
        }

        for script_type, required_terms in expected.items():
            with self.subTest(script_type=script_type):
                actual = {
                    item.strip()
                    for item in BRIEF_PRESETS[script_type]["required_content"].split("、")
                    if item.strip()
                }
                self.assertEqual(actual, required_terms)
        self.assertEqual(BRIEF_PRESETS["赛事"]["industry"], "城市综合挑战赛")

    def test_mutation_rejects_missing_session_token(self):
        response = self.client.post("/api/dialog/media")
        self.assertEqual(response.status_code, 403)

    def test_preflight_returns_machine_readable_checks(self):
        report = PreflightReport(
            brief={"项目名称": "测试"},
            project_dir=Path("/tmp/media"),
            output_dir=Path("/tmp/out"),
            checks=(CheckResult("测试项", "pass", "已通过"),),
        )
        payload = {
            "brief": {
                "project_name": "测试",
                "tone": "科技感",
                "required_content": "logo",
                "material_path": "/tmp/media",
            },
            "options": {"source_root": "/tmp/source", "output_dir": "/tmp/out"},
        }
        with patch("mvp_v2.local_ui.preflight_payload", return_value=report):
            response = self.client.post("/api/preflight", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["passed"])
        self.assertEqual(response.json()["checks"][0]["status"], "pass")

    def test_client_review_preflight_describes_model_failure_as_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            with patch("mvp_v2.preflight.minimax_credentials_available", return_value=True):
                report = run_launch_preflight(
                    brief_data=_brief(root),
                    source_root=source,
                    output_dir=root / "out",
                    asset_cache_root=root / "cache",
                    quality_profile="client-review",
                )

        message = next(check.message for check in report.checks if check.name == "文案生成")
        self.assertIn("失败会阻断", message)
        text_credentials = next(
            check for check in report.checks if check.name == "文本模型凭据"
        )
        self.assertEqual(text_credentials.status, "pass")

    def test_client_review_preflight_blocks_without_text_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            _write_source(source / "M+发布会类通用脚本库.xlsx")
            with patch("mvp_v2.preflight.minimax_credentials_available", return_value=False):
                report = run_launch_preflight(
                    brief_data=_brief(root),
                    source_root=source,
                    output_dir=root / "out",
                    asset_cache_root=root / "cache",
                    quality_profile="client-review",
                )

        text_credentials = next(
            check for check in report.checks if check.name == "文本模型凭据"
        )
        self.assertEqual(text_credentials.status, "fail")
        self.assertFalse(report.passed)

    def test_brief_form_allows_script_generation_without_project_media(self):
        brief = BriefForm(
            project_name="未来品牌发布会",
            client="未来品牌",
            tone="克制、现代",
            required_content="品牌logo、主会场",
        ).to_brief()

        self.assertEqual(brief["素材目录或素材文件"], "")
        self.assertEqual(brief["品牌或客户"], "未来品牌")

    def test_artifact_id_cannot_be_replaced_with_a_path(self):
        response = self.client.get("/api/artifacts/../../etc/passwd", headers=self.headers)
        self.assertIn(response.status_code, {404, 422})

    def test_review_import_endpoint_starts_the_same_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            review = root / "filled-review.xlsx"
            review.write_bytes(b"xlsx")
            run_dir = root / "cache" / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            parent_payload = _payload(root)
            queued = Job(job_id="review-job", payload=parent_payload)

            with patch("mvp_v2.local_ui.find_run_dir", return_value=run_dir), patch(
                "mvp_v2.local_ui.launch_payload_from_run",
                return_value=parent_payload,
            ), patch.object(
                self.app.state.jobs,
                "start",
                return_value=queued,
            ) as start:
                response = self.client.post(
                    "/api/history/run-1/review",
                    json={"path": str(review)},
                    headers=self.headers,
                )

        self.assertEqual(response.status_code, 200)
        submitted = start.call_args.args[0]
        self.assertEqual(submitted.options.resume_run_id, "run-1")
        self.assertEqual(submitted.options.review_workbook, str(review.resolve()))
        self.assertEqual(submitted.options.from_stage, "")
        self.assertEqual(submitted.options.retry_stage, "")


class JobStateTests(unittest.TestCase):
    def test_release_history_rebuilds_stale_portable_preview_from_final_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "cache" / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            workbook = run_dir / "final.xlsx"
            review = run_dir / "final.visual_review.html"
            report_path = run_dir / "run_report.json"
            review.write_text("stale", encoding="utf-8")
            workbook.write_bytes(b"new workbook")
            os.utime(review, (1, 1))
            os.utime(workbook, (2, 2))
            report = {
                "release_decision": "release_pass",
                "output_path": str(workbook),
                "visual_review_html": str(review),
            }

            def write_preview(source, target):
                self.assertEqual(Path(source), workbook)
                Path(target).write_text("portable", encoding="utf-8")
                return Path(target)

            with patch(
                "mvp_v2.local_ui.write_visual_review_html_from_workbook",
                side_effect=write_preview,
            ) as writer:
                result = portable_visual_review_path(report, report_path)
                repeated = portable_visual_review_path(report, report_path)
                self.assertEqual(result.read_text(encoding="utf-8"), "portable")
                self.assertEqual(repeated, result)
                writer.assert_called_once()

    def test_review_import_preflight_validates_contract_without_text_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "cache" / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            review = root / "filled-review.xlsx"
            review.write_bytes(b"xlsx")
            payload = _payload(root)
            payload.options.resume_run_id = "run-1"
            payload.options.review_workbook = str(review)

            with patch("mvp_v2.local_ui.find_run_dir", return_value=run_dir), patch(
                "mvp_v2.local_ui.validate_review_workbook",
                return_value={"source_run_id": "run-1"},
            ) as validate:
                report = preflight_payload(payload)

        self.assertTrue(report.passed, report.render())
        validate.assert_called_once_with(run_dir.parent.parent, review.resolve(), run_id="run-1")
        self.assertEqual([check.name for check in report.checks], ["人工复核表", "来源运行"])

    def test_exit_three_pending_review_is_not_a_program_failure(self):
        state, message = classify_report({
            "exit_code": 3,
            "machine_audit_status": "pass",
            "media_readiness_status": "not_ready",
            "human_review_status": "pending",
            "release_decision": "blocked",
        }, 3)
        self.assertEqual(state, "waiting_review")
        self.assertIn("成片素材待补", message)

    def test_release_pass_takes_precedence_over_resolved_historical_error(self):
        state, message = classify_report({
            "exit_code": 0,
            "status": "release_pass",
            "release_decision": "release_pass",
            "quality_review_status": "quality_pass",
            "reviewer_kind": "independent_ai",
            "error": {"message": "earlier retryable failure"},
        }, 0)

        self.assertEqual(state, "complete")
        self.assertIn("独立 AI", message)

    def test_history_reads_brief_from_resolved_current_report_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_root = root / "data"
            cache_root = data_root / "runs"
            cache_dir = cache_root / "测试项目"
            run_dir = cache_dir / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            report = {
                "run_id": "run-1",
                "exit_code": 3,
                "machine_audit_status": "pass",
                "human_review_status": "pending",
                "run_report_path": str(run_dir / "run_report.json"),
                "run_manifest_path": str(run_dir / "run_manifest.json"),
            }
            (run_dir / "run_report.json").write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8"
            )
            (run_dir / "run_manifest.json").write_text(
                json.dumps({"run_id": "run-1", "stages": {}}, ensure_ascii=False),
                encoding="utf-8",
            )
            (run_dir / "input_freeze.json").write_text(
                json.dumps({"brief": _brief(root)}, ensure_ascii=False), encoding="utf-8"
            )
            (cache_dir / "CURRENT").symlink_to(Path("runs") / "run-1")
            (cache_dir / "run_report.json").symlink_to(
                cache_dir / "CURRENT" / "run_report.json"
            )

            with patch.dict(os.environ, {"MPLUS_DATA_ROOT": str(data_root)}, clear=False):
                rows = scan_history(ArtifactRegistry())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["brief"]["project_name"], "测试发布会")

    def test_history_reads_run_from_read_only_legacy_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_root = root / "data"
            current_root = data_root / "runs"
            legacy_root = root / "archive" / "assets"
            run_dir = legacy_root / "旧项目" / "runs" / "run-legacy"
            run_dir.mkdir(parents=True)
            old_root = Path("/old/mplus/assets")
            output = run_dir / "out.xlsx"
            output.write_bytes(b"workbook")
            (run_dir / "run_manifest.json").write_text(
                json.dumps({"run_id": "run-legacy", "stages": {}}),
                encoding="utf-8",
            )
            (run_dir / "run_report.json").write_text(
                json.dumps({
                    "run_id": "run-legacy",
                    "exit_code": 3,
                    "machine_audit_status": "pass",
                    "human_review_status": "pending",
                    "run_manifest_path": str(old_root / "旧项目/runs/run-legacy/run_manifest.json"),
                    "output_path": str(old_root / "旧项目/runs/run-legacy/out.xlsx"),
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            data_root.mkdir(parents=True)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"data_root": str(data_root)}),
                encoding="utf-8",
            )
            (data_root / "legacy-mounts.json").write_text(
                json.dumps({"mounts": [{
                    "kind": "runs",
                    "mode": "read_only",
                    "original_path": str(old_root),
                    "path": str(legacy_root),
                }]}),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"MPLUS_CONFIG": str(config_path), "MPLUS_DATA_ROOT": ""},
                clear=False,
            ):
                with patch("mvp_v2.local_ui.DEFAULT_ASSET_CACHE_ROOT", current_root):
                    rows = scan_history(ArtifactRegistry())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "run-legacy")
        self.assertEqual(Path(rows[0]["report"]["output_path"]), output.resolve())
        self.assertTrue(any(item["kind"] == "workbook" for item in rows[0]["artifacts"]))

    def test_machine_failure_remains_a_generated_but_blocked_result(self):
        state, _ = classify_report({
            "exit_code": 2,
            "machine_audit_status": "fail",
            "human_review_status": "pending",
        }, 2)
        self.assertEqual(state, "machine_blocked")

    def test_manager_rejects_a_second_active_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = JobManager(ArtifactRegistry())
            payload = _payload(root)
            active = Job(job_id="active", payload=payload, state="running")
            manager._jobs[active.job_id] = active
            manager._active_job_id = active.job_id
            passed = PreflightReport(
                brief=payload.brief.to_brief(),
                project_dir=root / "media",
                output_dir=root / "out",
                checks=(CheckResult("测试", "pass", "ok"),),
            )
            with patch("mvp_v2.local_ui.preflight_payload", return_value=passed):
                with self.assertRaises(HTTPException) as context:
                    manager.start(payload)
            self.assertEqual(context.exception.status_code, 409)

    def test_cancel_sends_signal_to_the_current_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = JobManager(ArtifactRegistry())
            payload = _payload(root)
            process = Mock(pid=12345)
            process.poll.return_value = None
            job = Job(job_id="active", payload=payload, state="running", process=process)
            manager._jobs[job.job_id] = job
            manager._active_job_id = job.job_id
            with patch("mvp_v2.local_ui.os.killpg") as killpg:
                result = manager.cancel(job.job_id)
            self.assertEqual(result.state, "cancelling")
            killpg.assert_called_once_with(12345, signal.SIGTERM)

    def test_reads_all_eleven_stage_states_and_current_waiting_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "run_manifest.json"
            stages = {
                stage.value: {
                    "stage_id": stage.value,
                    "status": (
                        "succeeded"
                        if stage.value not in {"human_review", "publish_feedback"}
                        else (
                            "waiting_human_review"
                            if stage.value == "human_review"
                            else "not_started"
                        )
                    ),
                    "attempt": 1 if stage.value != "publish_feedback" else 0,
                    "error": None,
                }
                for stage in STAGE_ORDER
            }
            manifest_path.write_text(json.dumps({"stages": stages}), encoding="utf-8")

            progress, active = read_stage_progress({
                "run_manifest_path": str(manifest_path),
            })

            self.assertEqual(len(progress), 11)
            self.assertEqual(active["stage_id"], "human_review")
            self.assertEqual(active["status"], "waiting_human_review")
            self.assertEqual(stage_progress_summary(progress)["percent"], 82)

    def test_child_revision_may_start_from_parent_stage_without_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = JobManager(ArtifactRegistry())
            payload = _payload(root)
            payload.options.parent_run_id = "parent-1"
            payload.options.from_stage = "visual_binding"
            passed = PreflightReport(
                brief=payload.brief.to_brief(),
                project_dir=root / "media",
                output_dir=root / "out",
                checks=(CheckResult("测试", "pass", "ok"),),
            )
            with patch("mvp_v2.local_ui.preflight_payload", return_value=passed), patch.object(
                threading.Thread, "start", return_value=None
            ):
                job = manager.start(payload)

        self.assertEqual(job.payload.options.parent_run_id, "parent-1")
        self.assertEqual(job.payload.options.from_stage, "visual_binding")

    def test_job_command_passes_resume_and_recovery_stage_to_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = _payload(root)
            payload.options.resume_run_id = "run-123"
            payload.options.from_stage = "human_review"
            process = Mock()
            process.stdout = []
            process.wait.return_value = 3
            manager = JobManager(ArtifactRegistry())
            job = Job(job_id="resume-job", payload=payload)

            with patch(
                "mvp_v2.local_ui.project_cache_dir",
                return_value=root / "cache",
            ), patch(
                "mvp_v2.local_ui.subprocess.Popen",
                return_value=process,
            ) as popen:
                manager._run(job)

            command = popen.call_args.args[0]
            self.assertIn("--resume", command)
            self.assertEqual(command[command.index("--resume") + 1], "run-123")
            self.assertIn("--from-stage", command)
            self.assertEqual(
                command[command.index("--from-stage") + 1],
                "human_review",
            )

    def test_job_command_passes_review_workbook_to_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = _payload(root)
            review = root / "filled-review.xlsx"
            review.write_bytes(b"xlsx")
            payload.options.resume_run_id = "run-123"
            payload.options.review_workbook = str(review)
            process = Mock()
            process.stdout = []
            process.wait.return_value = 1
            manager = JobManager(ArtifactRegistry())
            job = Job(job_id="review-job", payload=payload)

            with patch(
                "mvp_v2.local_ui.project_cache_dir",
                return_value=root / "cache",
            ), patch(
                "mvp_v2.local_ui.subprocess.Popen",
                return_value=process,
            ) as popen:
                manager._run(job)

            command = popen.call_args.args[0]
            self.assertEqual(command[command.index("--resume") + 1], "run-123")
            self.assertEqual(
                command[command.index("--review-workbook") + 1],
                str(review),
            )

    def test_resumed_job_reads_the_updated_existing_run_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            run_dir = cache / "runs" / "run-123"
            run_dir.mkdir(parents=True)
            report_path = run_dir / "run_report.json"
            report_path.write_text(json.dumps({
                "run_id": "run-123",
                "exit_code": 3,
                "machine_audit_status": "pass",
                "human_review_status": "pending",
                "release_decision": "blocked",
            }), encoding="utf-8")
            payload = _payload(root)
            payload.options.resume_run_id = "run-123"
            process = Mock()
            process.stdout = []
            process.wait.return_value = 3
            manager = JobManager(ArtifactRegistry())
            job = Job(job_id="resume-job", payload=payload)

            with patch(
                "mvp_v2.local_ui.project_cache_dir",
                return_value=cache,
            ), patch(
                "mvp_v2.local_ui.subprocess.Popen",
                return_value=process,
            ), patch(
                "mvp_v2.local_ui.export_waiting_review_package",
                return_value=cache / "reviews" / "run-123-review.xlsx",
            ):
                manager._run(job)

        self.assertEqual(job.state, "waiting_review")
        self.assertEqual(job.report["run_id"], "run-123")

    def test_job_command_passes_quality_profile_and_omits_empty_project_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = _payload(root)
            payload.brief.material_path = ""
            payload.options.quality_profile = "client-review"
            process = Mock()
            process.stdout = []
            process.wait.return_value = 1
            manager = JobManager(ArtifactRegistry())
            job = Job(job_id="generic-brand-job", payload=payload)

            with patch(
                "mvp_v2.local_ui.project_cache_dir",
                return_value=root / "cache",
            ), patch(
                "mvp_v2.local_ui.subprocess.Popen",
                return_value=process,
            ) as popen:
                manager._run(job)

            command = popen.call_args.args[0]
            self.assertNotIn("--project-dir", command)
            self.assertEqual(
                command[command.index("--quality-profile") + 1],
                "client-review",
            )

    def test_waiting_run_exports_and_registers_review_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            workbook = run_dir / "未来品牌发布会-发布会脚本.xlsx"
            workbook.write_bytes(b"xlsx")
            report_path = run_dir / "run_report.json"
            report = {
                "run_id": "run-1",
                "output_path": str(workbook),
            }

            def fake_export(cache_dir, output, *, run_id):
                self.assertEqual(Path(cache_dir), root)
                self.assertEqual(run_id, "run-1")
                Path(output).write_bytes(b"review")
                return Path(output)

            with patch("mvp_v2.local_ui.export_review_workbook", side_effect=fake_export):
                review = export_waiting_review_package(root, report, report_path)
            artifacts = register_report_artifacts(ArtifactRegistry(), report, report_path)

        self.assertEqual(
            review.name,
            "run-1-未来品牌发布会-发布会脚本-人工复核.xlsx",
        )
        self.assertEqual(review.parent.name, "reviews")
        self.assertIn("review_workbook", {item["kind"] for item in artifacts})

    def test_history_registers_existing_user_named_review_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            workbook = run_dir / "未来品牌发布会-发布会脚本.xlsx"
            workbook.write_bytes(b"xlsx")
            report_path = run_dir / "run_report.json"
            report = {
                "run_id": "run-1",
                "output_path": str(workbook),
            }
            review = root / "reviews" / "run-1-客户复核稿-人工复核.xlsx"
            review.parent.mkdir(parents=True)
            review.write_bytes(b"filled-review")

            artifacts = register_report_artifacts(ArtifactRegistry(), report, report_path)

        review_artifact = next(item for item in artifacts if item["kind"] == "review_workbook")
        self.assertEqual(review_artifact["filename"], review.name)

    def test_waiting_run_preserves_existing_user_named_review_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            workbook = run_dir / "未来品牌发布会-发布会脚本.xlsx"
            workbook.write_bytes(b"xlsx")
            report_path = run_dir / "run_report.json"
            report = {
                "run_id": "run-1",
                "output_path": str(workbook),
            }
            review = root / "reviews" / "run-1-客户复核稿-人工复核.xlsx"
            review.parent.mkdir(parents=True)
            review.write_bytes(b"filled-review")

            with patch("mvp_v2.local_ui.export_review_workbook") as exporter:
                result = export_waiting_review_package(root, report, report_path)

            exporter.assert_not_called()
            self.assertEqual(result, review)
            self.assertEqual(review.read_bytes(), b"filled-review")

    def test_job_does_not_publish_terminal_state_before_review_artifact_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = _payload(root)
            run_dir = root / "cache" / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            workbook = run_dir / "result.xlsx"
            workbook.write_bytes(b"xlsx")
            report_path = run_dir / "run_report.json"
            report_path.write_text(json.dumps({
                "run_id": "run-1",
                "output_path": str(workbook),
                "machine_audit_status": "pass",
                "human_review_status": "pending",
                "release_decision": "blocked",
                "exit_code": 3,
            }), encoding="utf-8")
            process = Mock()
            process.stdout = []
            process.wait.return_value = 3
            manager = JobManager(ArtifactRegistry())
            job = Job(job_id="ordered-job", payload=payload)
            export_started = threading.Event()
            allow_export = threading.Event()

            def delayed_export(*args, **kwargs):
                export_started.set()
                allow_export.wait(2)
                review = root / "cache" / "reviews" / "run-1-result-人工复核.xlsx"
                review.parent.mkdir(parents=True, exist_ok=True)
                review.write_bytes(b"review")
                return review

            with patch(
                "mvp_v2.local_ui.project_cache_dir",
                return_value=root / "cache",
            ), patch(
                "mvp_v2.local_ui.subprocess.Popen",
                return_value=process,
            ), patch(
                "mvp_v2.local_ui.newest_run_report",
                return_value=report_path,
            ), patch(
                "mvp_v2.local_ui.export_waiting_review_package",
                side_effect=delayed_export,
            ):
                worker = threading.Thread(target=manager._run, args=(job,))
                worker.start()
                self.assertTrue(export_started.wait(1))
                self.assertEqual(job.state, "running")
                allow_export.set()
                worker.join(2)

        self.assertEqual(job.state, "waiting_review")
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
