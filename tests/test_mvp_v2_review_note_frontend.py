from pathlib import Path
import subprocess
import unittest


class ReviewNoteFrontendTests(unittest.TestCase):
    def test_unsent_review_notes_survive_only_their_current_review_items(self):
        """Run actual render/submit/load functions; this does not replace browser QA."""
        root = Path(__file__).parents[1]
        result = subprocess.run(
            ["node", str(root / "tests/support/review_note_frontend_probe.cjs")],
            cwd=root, check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 7)
        self.assertTrue(all(line.startswith("STEP_PASS|") for line in lines))
