from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CloudImageContractTests(unittest.TestCase):
    def test_cloud_image_pins_same_mmx_cli_as_local_baseline(self):
        dockerfile = (ROOT / "deploy" / "cloud" / "Dockerfile").read_text(
            encoding="utf-8",
        )

        self.assertIn("FROM node:24.14.0-bookworm-slim AS mmx", dockerfile)
        self.assertIn("FROM python:3.10.19-slim-bookworm", dockerfile)
        self.assertIn("npm install --global mmx-cli@1.0.16", dockerfile)
        self.assertIn('test "$(mmx --version)" = "mmx 1.0.16"', dockerfile)


if __name__ == "__main__":
    unittest.main()
