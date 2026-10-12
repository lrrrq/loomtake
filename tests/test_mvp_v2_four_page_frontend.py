from pathlib import Path
import subprocess
import unittest


class FourPageFrontendTests(unittest.TestCase):
    def test_explicit_actions_keep_authoritative_context(self):
        root = Path(__file__).parents[1]
        result = subprocess.run(
            ["node", str(root / "tests/support/four_page_frontend_probe.cjs")],
            cwd=root, check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.splitlines())
        self.assertTrue(all(line.startswith("STEP_PASS|") for line in result.stdout.splitlines()))
