import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mvp_v2.image_understanding import (
    VisionProviderError,
    _minimax_prompt,
    _run_minimax_vision,
    _script_type_for_asset,
    _tags_from_caption,
    generate_minimax_visual_analysis,
)
from mvp_v2.image_cli import main as image_cli_main


class ImageSecurityTests(unittest.TestCase):
    def test_image_search_cli_routes_non_launch_brief_and_slot_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.jsonl"
            analysis = Path(tmp) / "analysis.jsonl"
            manifest.write_text("", encoding="utf-8")
            analysis.write_text("", encoding="utf-8")
            with patch("mvp_v2.image_cli.search_images_for_slot") as search_mock:
                search_mock.return_value = {"candidates": []}
                with redirect_stdout(io.StringIO()):
                    exit_code = image_cli_main([
                        "search",
                        "--manifest", str(manifest),
                        "--analysis", str(analysis),
                        "--slot-text", "艺人拿起香氛试闻",
                        "--script-type", "明星活动",
                        "--shot-function", "产品互动",
                        "--visual-subject", "艺人与香氛产品",
                        "--industry", "品牌明星活动",
                        "--required-content", "明星出场、产品互动",
                        "--forbidden-content", "不出现电竞或跑步画面",
                    ])

        self.assertEqual(exit_code, 0)
        slot, brief, *_ = search_mock.call_args.args
        self.assertEqual(slot["镜头功能"], "产品互动")
        self.assertEqual(slot["画面主体"], "艺人与香氛产品")
        self.assertEqual(brief["脚本类型"], "明星活动")
        self.assertEqual(brief["行业或活动类别"], "品牌明星活动")
        self.assertIn("电竞", brief["禁忌或不能出现的内容"])

    def test_visual_prompt_uses_real_script_type_without_inventing_content(self):
        prompt = _minimax_prompt("音乐节")

        self.assertIn("音乐节脚本参考图", prompt)
        self.assertIn("不得因活动类别上下文猜测", prompt)
        self.assertNotIn("发布会剪辑脚本参考图", prompt)

    def test_visual_context_can_be_inferred_from_source_workbook(self):
        self.assertEqual(
            _script_type_for_asset({
                "来源文件": "M+明星活动类通用脚本库_AI版.xlsx",
                "原图路径": "/tmp/R7C4.png",
            }),
            "明星活动",
        )
        self.assertEqual(
            _script_type_for_asset({"活动类型": "展会", "来源文件": "unknown.xlsx"}),
            "展会",
        )

    def test_visual_salvage_tags_cover_non_launch_categories(self):
        tags = _tags_from_caption(
            "音乐节乐队演出，观众高举应援棒，舞台后方展台只是背景"
        )

        self.assertIn("音乐节", tags)
        self.assertIn("观众应援", tags)
        self.assertIn("展会", tags)

    def test_run_minimax_rejects_credential_carriers_and_payloads(self):
        image_path = Path("/tmp/example.png")
        commands = [
            ("python3 /tmp/fake.py --header='Authorization: Bearer header-value'", "header-value"),
            (
                "python3 /tmp/fake.py --headers='{\"Authorization\":\"Bearer json-header-value\"}'",
                "json-header-value",
            ),
            ("python3 /tmp/fake.py --credential-file /tmp/credential-value.json", "/tmp/credential-value.json"),
            ("python3 /tmp/fake.py --credentials=credential-value", "credential-value"),
            ("python3 /tmp/fake.py --auth-file /tmp/auth-value.json", "/tmp/auth-value.json"),
            ("python3 /tmp/fake.py --tokenfile /tmp/tokenfile-value.json", "/tmp/tokenfile-value.json"),
            ("python3 /tmp/fake.py --accesstoken=opaque-access-value", "opaque-access-value"),
            ("python3 /tmp/fake.py --authfile /tmp/authfile-value.json", "/tmp/authfile-value.json"),
        ]

        for command, credential_value in commands:
            with self.subTest(command=command):
                with patch("mvp_v2.image_understanding.subprocess.run") as run_mock:
                    run_mock.return_value = subprocess.CompletedProcess(
                        args=["python3", "/tmp/fake.py"],
                        returncode=1,
                        stdout="",
                        stderr=f"provider failed: {credential_value}",
                    )
                    with self.assertRaises(VisionProviderError) as context:
                        _run_minimax_vision(
                            command,
                            image_path,
                            "current-environment-key",
                            timeout_seconds=12,
                        )

                message = str(context.exception)
                self.assertIn("argv", message)
                self.assertIn("[REDACTED]", message)
                self.assertNotIn(credential_value, message)
                run_mock.assert_not_called()

    def test_run_minimax_rejects_all_credential_shaped_custom_arguments(self):
        image_path = Path("/tmp/example.png")
        commands = [
            ("python3 /tmp/fake.py --api-key other-api-secret", "other-api-secret"),
            ("python3 /tmp/fake.py --TOKEN=other-token-secret", "other-token-secret"),
            ("python3 /tmp/fake.py --clientSecret other-client-secret", "other-client-secret"),
            ("python3 /tmp/fake.py --db_password=other-password", "other-password"),
            ("python3 /tmp/fake.py --Authorization 'Bearer other-auth-secret'", "Bearer other-auth-secret"),
            ("python3 /tmp/fake.py --access-token-file /tmp/token.txt", "/tmp/token.txt"),
            ("python3 /tmp/fake.py --client-secret-file /tmp/secret.txt", "/tmp/secret.txt"),
            ("python3 /tmp/fake.py --authorization-header auth-value", "auth-value"),
            ("MINIMAX_API_KEY=other-env-secret python3 /tmp/fake.py", "other-env-secret"),
        ]

        for command, credential_value in commands:
            with self.subTest(command=command):
                with patch("mvp_v2.image_understanding.subprocess.run") as run_mock:
                    with self.assertRaises(VisionProviderError) as context:
                        _run_minimax_vision(
                            command,
                            image_path,
                            "current-environment-key",
                            timeout_seconds=12,
                        )

                run_mock.assert_not_called()
                message = str(context.exception)
                self.assertIn("argv", message)
                self.assertIn("[REDACTED]", message)
                self.assertNotIn(credential_value, message)

    def test_run_minimax_allows_noncredential_custom_arguments(self):
        image_path = Path("/tmp/example.png")

        with patch("mvp_v2.image_understanding.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["python3", "/tmp/fake.py"],
                returncode=0,
                stdout="ok",
                stderr="",
            )

            _run_minimax_vision(
                "python3 /tmp/fake.py --model MiniMax-M2.7 --max-tokens 100 "
                "--tokenizer /tmp/tokenizer.json --timeout 30 --output json",
                image_path,
                "current-environment-key",
                timeout_seconds=12,
            )

        run_mock.assert_called_once()

    def test_run_minimax_passes_api_key_via_env_not_argv(self):
        image_path = Path("/tmp/example.png")

        with patch("mvp_v2.image_understanding.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["mmx"],
                returncode=0,
                stdout='{"caption":"ok"}',
                stderr="",
            )

            _run_minimax_vision("mmx", image_path, "minimax-secret", timeout_seconds=12)

        called_args = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
        self.assertIn("--non-interactive", called_args)
        self.assertNotIn("minimax-secret", called_args)
        self.assertEqual(run_mock.call_args.kwargs["env"]["MINIMAX_API_KEY"], "minimax-secret")

    def test_run_minimax_does_not_append_plaintext_key_to_custom_command(self):
        image_path = Path("/tmp/example.png")

        with patch("mvp_v2.image_understanding.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["python3", "/tmp/fake.py", "--mode", "dry-run"],
                returncode=0,
                stdout="ok",
                stderr="",
            )

            _run_minimax_vision(
                "python3 /tmp/fake.py --mode dry-run",
                image_path,
                "minimax-secret",
                timeout_seconds=12,
            )

        called_args = run_mock.call_args.kwargs["args"] if "args" in run_mock.call_args.kwargs else run_mock.call_args.args[0]
        self.assertEqual(called_args, ["python3", "/tmp/fake.py", "--mode", "dry-run"])
        self.assertNotIn("minimax-secret", called_args)
        self.assertEqual(run_mock.call_args.kwargs["env"]["MINIMAX_API_KEY"], "minimax-secret")

    def test_run_minimax_rejects_custom_command_that_already_contains_same_api_key(self):
        image_path = Path("/tmp/example.png")

        with patch("mvp_v2.image_understanding.subprocess.run") as run_mock:
            with self.assertRaises(VisionProviderError) as context:
                _run_minimax_vision(
                    "python3 /tmp/fake.py --api-key minimax-secret",
                    image_path,
                    "minimax-secret",
                    timeout_seconds=12,
                )

        run_mock.assert_not_called()
        self.assertIn("argv", str(context.exception))
        self.assertNotIn("minimax-secret", str(context.exception))
        self.assertIn("[REDACTED]", str(context.exception))

    def test_run_minimax_redacts_secret_from_provider_failure(self):
        image_path = Path("/tmp/example.png")

        with patch("mvp_v2.image_understanding.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess(
                args=["mmx"],
                returncode=1,
                stdout="",
                stderr="provider failed: minimax-secret",
            )

            with self.assertRaises(VisionProviderError) as context:
                _run_minimax_vision("mmx", image_path, "minimax-secret", timeout_seconds=12)

        self.assertNotIn("minimax-secret", str(context.exception))
        self.assertIn("[REDACTED]", str(context.exception))

    def test_generate_minimax_redacts_secret_when_command_is_missing(self):
        with patch("mvp_v2.image_understanding._command_available", return_value=False):
            with self.assertRaises(VisionProviderError) as context:
                generate_minimax_visual_analysis(
                    [],
                    api_key="minimax-secret",
                    command="/tmp/not-present --api-key minimax-secret",
                )

        self.assertNotIn("minimax-secret", str(context.exception))
        self.assertIn("[REDACTED]", str(context.exception))

    def test_generate_minimax_rejects_unknown_credential_before_command_lookup(self):
        with patch("mvp_v2.image_understanding._command_available") as available_mock:
            with self.assertRaises(VisionProviderError) as context:
                generate_minimax_visual_analysis(
                    [],
                    api_key="current-environment-key",
                    command="/tmp/not-present --password unrelated-command-secret",
                )

        available_mock.assert_not_called()
        message = str(context.exception)
        self.assertIn("[REDACTED]", message)
        self.assertNotIn("unrelated-command-secret", message)
