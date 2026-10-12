import unittest

from scripts.source_publish import (
    MAX_FIXTURE_IMAGE_BYTES,
    boundary_issues,
    is_publish_path,
    SOURCE_COMMIT_PATTERN,
    RELEASE_VERSION_PATTERN,
)


class SourcePublishTests(unittest.TestCase):
    def test_release_metadata_requires_full_source_sha_and_version(self):
        message = (
            "chore: publish source-only snapshot\n\n"
            "Release-Version: 1.1.0\n"
            f"Source-Commit: {'a' * 40}\n"
        )

        self.assertEqual(SOURCE_COMMIT_PATTERN.search(message).group(1), "a" * 40)
        self.assertEqual(RELEASE_VERSION_PATTERN.search(message).group(1), "1.1.0")
        self.assertIsNone(SOURCE_COMMIT_PATTERN.search("Source-Commit: abc123"))

    def test_publish_allowlist_excludes_business_documents(self):
        self.assertTrue(is_publish_path("mvp_v2/local_ui.py"))
        self.assertTrue(is_publish_path("README.md"))
        self.assertFalse(is_publish_path("各需求文档/客户需求.md"))
        self.assertFalse(is_publish_path("方案/runtime/result.json"))

    def test_boundary_allows_only_small_fixture_images(self):
        paths = [
            "mvp_v2/local_ui.py",
            "tests/fixtures/anonymous.png",
        ]

        self.assertEqual(
            boundary_issues(paths, {"tests/fixtures/anonymous.png": 128}),
            [],
        )
        self.assertEqual(
            boundary_issues(
                ["tests/fixtures/large.png"],
                {"tests/fixtures/large.png": MAX_FIXTURE_IMAGE_BYTES + 1},
            ),
            ["fixture_image_too_large:tests/fixtures/large.png"],
        )
        self.assertEqual(
            boundary_issues(["docs/screenshot.png"], {"docs/screenshot.png": 128}),
            ["non_fixture_image:docs/screenshot.png"],
        )

    def test_boundary_rejects_private_and_generated_files(self):
        paths = [
            ".env",
            "mvp_v2/runtime/state.json",
            "tests/fixtures/customer.xlsx",
        ]

        self.assertEqual(
            boundary_issues(paths, {}),
            [
                "secret_config:.env",
                "local_or_generated_path:mvp_v2/runtime/state.json",
                "forbidden_binary:tests/fixtures/customer.xlsx",
            ],
        )


if __name__ == "__main__":
    unittest.main()
