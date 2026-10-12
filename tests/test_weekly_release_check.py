from pathlib import Path
from unittest.mock import patch

from scripts.weekly_release_check import weekly_release_report


def test_weekly_check_uses_stable_commit_as_approved_source(tmp_path: Path):
    stable = tmp_path / "stable"
    dev = tmp_path / "dev"
    (stable / ".git").mkdir(parents=True)
    (dev / ".git").mkdir(parents=True)
    stable_sha = "a" * 40
    dev_sha = "b" * 40

    def fake_git(cwd: Path, *args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return stable_sha if cwd == stable else dev_sha
        if args[:2] == ("rev-list", "--count"):
            return "1"
        if args[:2] == ("status", "--porcelain"):
            return ""
        raise AssertionError((cwd, args))

    with (
        patch("scripts.weekly_release_check._git", side_effect=fake_git),
        patch("scripts.weekly_release_check.source_status") as status,
    ):
        status.return_value = {
            "verdict": "PASS",
            "issues": [],
            "publish": {
                "publish_commit": "c" * 40,
                "source_commit": stable_sha,
            },
        }
        report = weekly_release_report(stable_worktree=stable, dev_worktree=dev)

    status.assert_called_once_with(stable_sha)
    assert report["verdict"] == "PASS"
    assert report["dev_commits_ahead"] == 1
