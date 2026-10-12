from pathlib import Path
import shutil
import subprocess
import unittest


class ImageDisplayTests(unittest.TestCase):
    def test_current_reference_pending_and_cancelled_images_use_distinct_semantics(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node runtime unavailable")
        result = subprocess.run([node, str(Path(__file__).parent / "support/formal_image_display_probe.cjs")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
