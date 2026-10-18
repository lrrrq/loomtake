from pathlib import Path
import subprocess
import unittest


class ScriptReadingFrontendTests(unittest.TestCase):
    def test_reading_copy_search_and_draft_consistency(self):
        root = Path(__file__).parents[1]
        result = subprocess.run(
            ['node', str(root / 'tests/support/script_reading_probe.cjs')],
            cwd=root, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 5)
