from pathlib import Path
import subprocess
import unittest


class FormalUsabilityFrontendTests(unittest.TestCase):
    def test_navigation_image_drafts_and_agent_async_context(self):
        """Run actual app functions without a browser; rendered journeys need separate QA."""
        root = Path(__file__).parents[1]
        result = subprocess.run(
            ["node", str(root / "tests/support/formal_usability_probe.cjs")],
            cwd=root, check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 35)
        self.assertTrue(all(line.startswith("STEP_PASS|") for line in result.stdout.splitlines()))
