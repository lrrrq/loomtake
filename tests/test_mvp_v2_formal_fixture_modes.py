import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from mvp_v2 import formal_runtime


class FormalFixtureModeTests(unittest.TestCase):
    def _environment(self, root, mode):
        return {"MPLUS_FORMAL_FIXTURE_ROOT": str(root), "MPLUS_FORMAL_FIXTURE_MODE": mode,
            "MPLUS_FORMAL_FIXTURE_SCENARIO": "default", "MPLUS_FORMAL_REAL_DEEPSEEK": "0",
            "MPLUS_FORMAL_REAL_MINIMAX": "0", "MPLUS_FORMAL_REAL_ADVISOR_ONLY": "0"}

    def test_real_mode_uses_current_production_composition_and_no_manual_image_metadata(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, self._environment(temporary, "real")), patch(
            "mvp_v2.runtime_config.load_local_secrets",
        ), patch("mvp_v2.formal_runtime.provider_credentials_available", return_value=True), patch(
            "mvp_v2.formal_runtime.DEEPSEEK_WRITER_MAX_OUTPUT_TOKENS", 1536,
        ), patch("mvp_v2.formal_runtime.DEEPSEEK_EDITOR_MAX_OUTPUT_TOKENS", 2304):
            calls = []
            with patch("mvp_v2.formal_runtime.build_text_provider", return_value=lambda prompt, model, limit: calls.append(limit) or "{}"):
                fixture = runpy.run_module("tests.support.formal_console_fixture_server", run_name="fixture_mode_probe")
            self.assertEqual(calls, [], "startup must not spend provider calls")
            self.assertIsNotNone(fixture["adapter"].understanding_policy)
            fixture["generation"].provider_policy.writer("{}", "anonymous", 65536)
            fixture["generation"].provider_policy.editor("{}\nDRAFT:{}", "anonymous", 65536)
            self.assertEqual(calls, [1536, 2304], "test must follow changed production limits, not its own constants")
            self.assertFalse((Path(temporary) / "asset-metadata" / "image_analysis.minimax.jsonl").exists())
            self.assertEqual(json.loads((Path(temporary) / "fixture-mode.json").read_text())["mode"], "real")
            self.assertEqual(fixture["agent"].services.store.database_path,
                fixture["adapter"].service.store.database_path)

    def test_deterministic_mode_preserves_fixture_semantics_without_production_provider(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, self._environment(temporary, "deterministic")), patch(
            "mvp_v2.formal_runtime.build_formal_runtime", side_effect=AssertionError("deterministic mode must stay isolated"),
        ):
            fixture = runpy.run_module("tests.support.formal_console_fixture_server", run_name="fixture_mode_probe")
            self.assertEqual(fixture["generation"].provider_policy.writer_provider, "anonymous-fixture-writer")
            self.assertTrue((Path(temporary) / "asset-metadata" / "image_analysis.minimax.jsonl").exists())

    def test_real_mode_rejects_an_existing_unlabelled_or_deterministic_root(self):
        for marked in (False, True):
            with self.subTest(marked=marked), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "old-fixture.txt").write_text("existing evidence")
                if marked:
                    (root / "fixture-mode.json").write_text(json.dumps({"mode": "deterministic"}))
                with patch.dict(os.environ, self._environment(root, "real")):
                    with self.assertRaisesRegex(RuntimeError, "独立|模式"):
                        runpy.run_module("tests.support.formal_console_fixture_server", run_name="fixture_mode_probe")
                self.assertEqual((root / "old-fixture.txt").read_text(), "existing evidence")


if __name__ == "__main__":
    unittest.main()
