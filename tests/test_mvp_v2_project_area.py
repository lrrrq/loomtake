from pathlib import Path
import tempfile
import unittest

from mvp_v2.project_area import (
    ProjectAreaRecord,
    broken_root_symlinks,
    project_root_issues,
    read_records,
    remove_registered_broken_symlinks,
    write_records,
)


class ProjectAreaTests(unittest.TestCase):
    def test_check_allows_registered_source_and_local_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".context").mkdir()
            (root / ".DS_Store").write_bytes(b"local")
            (root / ".dockerignore").write_text(".git\n", encoding="utf-8")
            (root / "deploy").mkdir()
            (root / "mplus").write_text("#!/bin/zsh\n", encoding="utf-8")

            self.assertEqual(project_root_issues(root), [])

    def test_transitional_check_allows_registered_legacy_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("docs", "mvp_v2", "tests", "schemas", "各需求文档", "方案", "整理后脚本库"):
                (root / name).mkdir()

            self.assertEqual(project_root_issues(root), [])
            self.assertIn("unexpected_root_entry:整理后脚本库", project_root_issues(root, final=True))

    def test_final_check_allows_only_markdown_inside_plan_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans = root / "方案"
            plans.mkdir()
            (plans / "readme.md").write_text("docs", encoding="utf-8")
            self.assertEqual(project_root_issues(root, final=True), [])
            (plans / "runtime").mkdir()
            self.assertIn("unexpected_plan_entry:runtime", project_root_issues(root, final=True))

    def test_check_rejects_new_business_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mvp_v2").mkdir()
            (root / "customer.xlsx").write_bytes(b"test")

            self.assertEqual(
                project_root_issues(root),
                ["unexpected_root_entry:customer.xlsx"],
            )

    def test_broken_links_are_manifested_before_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            link = root / "old.xlsx"
            link.symlink_to(root / "missing.xlsx")
            manifest = root / "manifest.jsonl"

            records = broken_root_symlinks(root)
            write_records(manifest, records)
            loaded = read_records(manifest)
            removed = remove_registered_broken_symlinks(root, loaded)

            self.assertEqual(removed, ["old.xlsx"])
            self.assertFalse(link.is_symlink())
            self.assertEqual(loaded[0].link_target, str(root / "missing.xlsx"))

    def test_changed_link_is_not_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            link = root / "old.xlsx"
            link.symlink_to(root / "missing.xlsx")
            record = ProjectAreaRecord(
                path="old.xlsx",
                kind="symlink",
                size=1,
                mtime_ns=1,
                link_target=str(root / "different.xlsx"),
            )

            with self.assertRaisesRegex(ValueError, "已变化"):
                remove_registered_broken_symlinks(root, [record])


if __name__ == "__main__":
    unittest.main()
