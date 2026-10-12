from pathlib import Path
import tempfile
import unittest

from mvp_v2.package_usage import append_local_usage, local_usage_summary


class PackageUsageTests(unittest.TestCase):
    def test_local_usage_keeps_locked_version_and_unknown_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            append_local_usage(root, {
                "requested_ref": "launch-core@formal",
                "package_id": "launch-core",
                "package_version": 4,
                "package_sha256": "a" * 64,
                "status": "succeeded",
            })
            append_local_usage(root, {
                "requested_ref": "unknown",
                "package_id": "unknown",
                "package_version": None,
                "package_sha256": "unknown",
                "status": "failed",
            })

            rows = local_usage_summary(root)

            self.assertEqual(sum(row["call_count"] for row in rows), 2)
            unknown = [row for row in rows if row["package_id"] == "unknown"][0]
            self.assertIsNone(unknown["package_version"])
