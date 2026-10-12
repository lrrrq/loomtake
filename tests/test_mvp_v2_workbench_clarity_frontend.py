from pathlib import Path
import subprocess
import unittest


class WorkbenchClarityFrontendTests(unittest.TestCase):
    def test_user_actions_keep_authoritative_context(self):
        root = Path(__file__).parents[1]
        result = subprocess.run(
            ["node", str(root / "tests/support/workbench_clarity_probe.cjs")],
            cwd=root, check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.splitlines())
        self.assertTrue(all(line.startswith("STEP_PASS|") for line in result.stdout.splitlines()))
