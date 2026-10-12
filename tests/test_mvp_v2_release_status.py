import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mvp_v2.release_status import version_report


class ReleaseStatusTests(unittest.TestCase):
    def test_version_report_exposes_profile_backend_and_locked_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {
                "MPLUS_PROFILE": "test",
                "MPLUS_DATA_ROOT": str(root),
                "MPLUS_EXECUTION_BACKEND": "local",
                "MPLUS_SOURCE_COMMIT": "a" * 40,
            }, clear=False):
                report = version_report()

        self.assertEqual(report["runner_version"], "1.1.3")
        self.assertEqual(report["source_commit"], "a" * 40)
        self.assertEqual(report["profile"], "test")
        self.assertEqual(report["execution_backend"], "local")
        self.assertEqual(report["formal_packages"], [])
        self.assertIn("source_dirty", report)
