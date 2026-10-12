from pathlib import Path
import subprocess
import unittest


class TemplateFrontendTests(unittest.TestCase):
    def test_template_inputs_authority_and_async_ownership(self):
        """Exercise actual client functions; browser journeys validate rendered interactions."""
        root = Path(__file__).parents[1]
        result = subprocess.run(
            ["node", str(root / "tests/support/template_frontend_probe.cjs")],
            cwd=root, check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 12)
        self.assertTrue(all(line.startswith("STEP_PASS|") for line in lines))
