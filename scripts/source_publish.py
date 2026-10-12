#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mvp_v2.run_contract import sha256_json


REMOTE_NAME = "publish"
PUBLISH_BRANCH = "main"
ROOT_FILES = {
    ".gitattributes",
    ".dockerignore",
    ".gitignore",
    ".python-version",
    "AGENTS.md",
    "README.md",
    "mplus",
    "pyproject.toml",
    "requirements-ui.txt",
    "uv.lock",
    "启动M+脚本工具.command",
    "启动发布会工具.command",
}
SOURCE_DIRECTORIES = {"deploy", "docs", "mvp", "mvp_v2", "schemas", "scripts", "tests"}
PATHSPECS = tuple(sorted(ROOT_FILES | SOURCE_DIRECTORIES))
FORBIDDEN_SUFFIXES = {
    ".db",
    ".env",
    ".mp4",
    ".mov",
    ".pdf",
    ".sqlite3",
    ".xls",
    ".xlsm",
    ".xlsx",
}
IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png"}
FORBIDDEN_PARTS = {
    ".codegraph",
    ".context",
    ".omc",
    ".superpowers",
    "cache",
    "data",
    "outputs",
    "reports",
    "runs",
    "runtime",
}
MAX_FIXTURE_IMAGE_BYTES = 1024 * 1024
SOURCE_COMMIT_PATTERN = re.compile(r"(?m)^Source-Commit:\s*([0-9a-f]{40})\s*$")
RELEASE_VERSION_PATTERN = re.compile(r"(?m)^Release-Version:\s*([^\s]+)\s*$")
SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


class PublishError(RuntimeError):
    pass


def _git(*args: str, cwd: Path = PROJECT_ROOT, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout


def is_publish_path(path: str) -> bool:
    item = PurePosixPath(path)
    return path in ROOT_FILES or (bool(item.parts) and item.parts[0] in SOURCE_DIRECTORIES)


def boundary_issues(paths: list[str], sizes: dict[str, int]) -> list[str]:
    issues = []
    for value in paths:
        path = PurePosixPath(value)
        fixture = path.parts[:2] == ("tests", "fixtures")
        suffix = path.suffix.casefold()
        if path.name == ".env" or path.name.startswith(".env."):
            issues.append(f"secret_config:{value}")
        elif any(part in FORBIDDEN_PARTS for part in path.parts):
            issues.append(f"local_or_generated_path:{value}")
        elif suffix in FORBIDDEN_SUFFIXES:
            issues.append(f"forbidden_binary:{value}")
        elif suffix in IMAGE_SUFFIXES and not fixture:
            issues.append(f"non_fixture_image:{value}")
        elif suffix in IMAGE_SUFFIXES and sizes.get(value, 0) > MAX_FIXTURE_IMAGE_BYTES:
            issues.append(f"fixture_image_too_large:{value}")
    return issues


def _resolved_commit(source_ref: str) -> str:
    try:
        return _git("rev-parse", "--verify", f"{source_ref}^{{commit}}").strip()
    except subprocess.CalledProcessError as exc:
        raise PublishError(f"invalid_source_ref:{source_ref}") from exc


def _source_paths(source_ref: str) -> list[str]:
    raw = _git(
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        source_ref,
        "--",
        *PATHSPECS,
        text=False,
    )
    return [item.decode("utf-8") for item in raw.split(b"\0") if item]


def _blob_sizes(source_ref: str, paths: list[str]) -> dict[str, int]:
    sizes = {}
    for path in paths:
        if PurePosixPath(path).suffix.casefold() not in IMAGE_SUFFIXES:
            continue
        output = _git("cat-file", "-s", f"{source_ref}:{path}")
        sizes[path] = int(output.strip())
    return sizes


def _dirty_publish_paths() -> list[str]:
    raw = _git(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        *PATHSPECS,
        text=False,
    )
    return [item[3:].decode("utf-8") for item in raw.split(b"\0") if item]


def check_source(source_ref: str = "HEAD") -> dict:
    source_commit = _resolved_commit(source_ref)
    paths = _source_paths(source_commit)
    issues = boundary_issues(paths, _blob_sizes(source_commit, paths))
    if source_ref == "HEAD":
        issues.extend(f"uncommitted_publish_path:{path}" for path in _dirty_publish_paths())
    try:
        remote_url = _git("remote", "get-url", REMOTE_NAME).strip()
    except subprocess.CalledProcessError:
        remote_url = ""
        issues.append(f"missing_remote:{REMOTE_NAME}")
    return {
        "status": "pass" if not issues else "blocked",
        "source_ref": source_ref,
        "source_commit": source_commit,
        "remote": REMOTE_NAME,
        "remote_url": remote_url,
        "file_count": len(paths),
        "issues": sorted(set(issues)),
    }


def _extract_archive(archive: bytes, target: Path) -> None:
    root = target.resolve()
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            destination = (target / member.name).resolve()
            if destination != root and root not in destination.parents:
                raise PublishError(f"archive_path_escape:{member.name}")
            if member.issym() or member.islnk():
                raise PublishError(f"archive_link_not_allowed:{member.name}")
        bundle.extractall(target)


def remote_head(remote_url: str) -> str:
    try:
        output = _git("ls-remote", "--heads", remote_url, PUBLISH_BRANCH).strip()
    except subprocess.CalledProcessError as exc:
        raise PublishError("remote_unavailable") from exc
    rows = [line.split() for line in output.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2:
        raise PublishError("remote_branch_missing_or_ambiguous")
    return rows[0][0]


def _remote_metadata(remote_url: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="mplus-publish-status-") as temporary:
        target = Path(temporary) / "snapshot"
        try:
            subprocess.run(
                [
                    "git", "clone", "--quiet", "--depth", "1", "--filter=blob:none",
                    "--no-checkout", "--single-branch", "--branch", PUBLISH_BRANCH,
                    remote_url, str(target),
                ],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise PublishError("remote_metadata_unavailable") from exc
        commit = _git("rev-parse", "HEAD", cwd=target).strip()
        message = _git("show", "-s", "--format=%B", "HEAD", cwd=target)
    source_match = SOURCE_COMMIT_PATTERN.search(message)
    release_match = RELEASE_VERSION_PATTERN.search(message)
    return {
        "publish_commit": commit,
        "source_commit": source_match.group(1) if source_match else "",
        "release_version": release_match.group(1) if release_match else "",
    }


def source_status(source_ref: str = "HEAD") -> dict:
    audit = check_source(source_ref)
    issues = list(audit["issues"])
    if not audit["remote_url"]:
        return {**audit, "verdict": "BLOCK", "issues": sorted(set(issues))}
    metadata = _remote_metadata(audit["remote_url"])
    if not metadata["source_commit"]:
        issues.append("remote_missing_source_commit")
    if issues:
        verdict = "BLOCK"
    elif metadata["source_commit"] == audit["source_commit"]:
        verdict = "PASS"
    else:
        verdict = "REVISE"
    return {
        **audit,
        "status": "pass" if verdict == "PASS" else "blocked" if verdict == "BLOCK" else "behind",
        "verdict": verdict,
        "issues": sorted(set(issues)),
        "publish": metadata,
    }


def _release_version(source_commit: str, explicit: str | None) -> str:
    if explicit:
        version = explicit.strip()
    else:
        try:
            payload = _git("show", f"{source_commit}:mvp_v2/__init__.py")
        except subprocess.CalledProcessError as exc:
            raise PublishError("runner_version_unavailable") from exc
        match = re.search(r'(?m)^__version__\s*=\s*["\']([^"\']+)["\']\s*$', payload)
        version = match.group(1) if match else ""
    if not SEMVER_PATTERN.fullmatch(version):
        raise PublishError(f"invalid_release_version:{version}")
    return version


def prepare_snapshot(
    output: Path,
    *,
    source_ref: str = "HEAD",
    expected_remote: str | None = None,
    release_version: str | None = None,
) -> dict:
    audit = check_source(source_ref)
    if audit["status"] != "pass":
        raise PublishError(json.dumps(audit, ensure_ascii=False, sort_keys=True))
    current_remote = remote_head(audit["remote_url"])
    expected_commit = ""
    if expected_remote:
        expected_commit = (
            expected_remote.casefold()
            if re.fullmatch(r"[0-9a-fA-F]{40}", expected_remote)
            else _resolved_commit(expected_remote)
        )
    if expected_commit and current_remote != expected_commit:
        raise PublishError(
            f"remote_head_drift:expected={expected_commit}:actual={current_remote}"
        )
    version = _release_version(audit["source_commit"], release_version)
    target = output.expanduser().resolve()
    if target.exists():
        raise PublishError(f"output_already_exists:{target}")
    subprocess.run(
        [
            "git",
            "clone",
            "--single-branch",
            "--branch",
            PUBLISH_BRANCH,
            audit["remote_url"],
            str(target),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    _git("rm", "-r", "--ignore-unmatch", ".", cwd=target)
    archive = _git(
        "archive",
        "--format=tar",
        audit["source_commit"],
        "--",
        *PATHSPECS,
        text=False,
    )
    _extract_archive(archive, target)
    _git("add", "-A", cwd=target)
    staged = _git("diff", "--cached", "--name-only", "-z", cwd=target, text=False)
    staged_paths = [item.decode("utf-8") for item in staged.split(b"\0") if item]
    issues = boundary_issues(
        [path for path in staged_paths if (target / path).is_file()],
        {
            path: (target / path).stat().st_size
            for path in staged_paths
            if (target / path).is_file()
        },
    )
    if issues:
        raise PublishError(json.dumps({"status": "blocked", "issues": issues}, ensure_ascii=False))
    return {
        **audit,
        "status": "ready",
        "output": str(target),
        "publish_base": _git("rev-parse", "HEAD", cwd=target).strip(),
        "release_version": version,
        "staged_path_count": len(staged_paths),
        "commit_message": (
            "chore: publish source-only snapshot\n\n"
            f"Release-Version: {version}\n"
            f"Source-Commit: {audit['source_commit']}"
        ),
    }


def verify_snapshot(path: Path) -> dict:
    target = path.expanduser().resolve()
    if not (target / ".git").exists():
        raise PublishError(f"snapshot_not_git_repository:{target}")
    if _git("status", "--porcelain", cwd=target).strip():
        raise PublishError("snapshot_worktree_not_clean")
    message = _git("show", "-s", "--format=%B", "HEAD", cwd=target)
    source_match = SOURCE_COMMIT_PATTERN.search(message)
    release_match = RELEASE_VERSION_PATTERN.search(message)
    if not source_match:
        raise PublishError("snapshot_missing_source_commit")
    if not release_match:
        raise PublishError("snapshot_missing_release_version")
    runner_version = ""
    init_path = target / "mvp_v2" / "__init__.py"
    if init_path.is_file():
        match = re.search(
            r'(?m)^__version__\s*=\s*["\']([^"\']+)["\']\s*$',
            init_path.read_text(encoding="utf-8"),
        )
        runner_version = match.group(1) if match else ""
    if runner_version != release_match.group(1):
        raise PublishError(
            f"snapshot_release_version_mismatch:{release_match.group(1)}:{runner_version}"
        )
    tracked = [
        item.decode("utf-8")
        for item in _git("ls-files", "-z", cwd=target, text=False).split(b"\0")
        if item
    ]
    issues = boundary_issues(
        tracked,
        {item: (target / item).stat().st_size for item in tracked if (target / item).is_file()},
    )
    if issues:
        raise PublishError(json.dumps({"boundary_issues": issues}, ensure_ascii=False))
    checks = (
        ("git_fsck", ["git", "fsck", "--full"]),
        ("lock", ["uv", "lock", "--check"]),
        (
            "tests",
            ["uv", "run", "--locked", "--python", "3.10", "python", "-m", "pytest", "-q"],
        ),
        (
            "guard",
            [
                "uv", "run", "--locked", "--no-dev", "--python", "3.10",
                "python", "scripts/project_area_guard.py", "check",
            ],
        ),
        ("doctor", ["./mplus", "doctor"]),
        ("minimal_start", ["./mplus", "version"]),
    )
    evidence = []
    for name, command in checks:
        completed = subprocess.run(
            command,
            cwd=target,
            capture_output=True,
            text=True,
            check=False,
        )
        combined = "\n".join(
            line for line in (completed.stdout + "\n" + completed.stderr).splitlines() if line.strip()
        )
        evidence.append({
            "name": name,
            "exit_code": completed.returncode,
            "final_output": combined.splitlines()[-1] if combined else "",
        })
        if completed.returncode != 0:
            raise PublishError(json.dumps({"failed_check": name, "evidence": evidence}, ensure_ascii=False))
    return {
        "status": "pass",
        "publish_commit": _git("rev-parse", "HEAD", cwd=target).strip(),
        "source_commit": source_match.group(1),
        "release_version": release_match.group(1),
        "tracked_file_count": len(tracked),
        "checks": evidence,
    }


def write_release_receipt(
    snapshot: Path,
    output: Path,
    *,
    package_registry: Path | None = None,
    container_image_digest: str = "",
) -> dict:
    verification = verify_snapshot(snapshot)
    try:
        remote_url = _git("remote", "get-url", REMOTE_NAME).strip()
    except subprocess.CalledProcessError as exc:
        raise PublishError(f"missing_remote:{REMOTE_NAME}") from exc
    actual_remote = remote_head(remote_url)
    if actual_remote != verification["publish_commit"]:
        raise PublishError(
            f"remote_publish_mismatch:expected={verification['publish_commit']}:actual={actual_remote}"
        )
    formal_packages = []
    if package_registry:
        payload = json.loads(package_registry.expanduser().resolve().read_text(encoding="utf-8"))
        packages = dict(payload.get("packages") or {})
        for category_id, aliases in sorted(dict(payload.get("aliases") or {}).items()):
            ref = str(dict(aliases or {}).get("formal") or "")
            record = dict(packages.get(ref) or {})
            formal_packages.append({
                "category_id": category_id,
                "formal_ref": ref,
                "package_id": record.get("package_id"),
                "package_version": record.get("package_version"),
                "package_sha256": record.get("package_sha256"),
            })
    image_digest = str(container_image_digest or "unavailable").strip().casefold()
    if image_digest != "unavailable" and not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        raise PublishError("invalid_container_image_digest")
    receipt = {
        "schema_version": "mplus-source-release-receipt/v1",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_commit": verification["source_commit"],
        "publish_commit": verification["publish_commit"],
        "release_version": verification["release_version"],
        "postgres_schema_version": _postgres_schema_version(snapshot),
        "container_image_digest": image_digest,
        "formal_packages": formal_packages,
        "verification": verification["checks"],
        "verification_sha256": sha256_json(verification["checks"]),
        "remote": {"name": REMOTE_NAME, "url": remote_url, "head": actual_remote},
    }
    receipt["receipt_sha256"] = sha256_json(receipt)
    target = output.expanduser().resolve()
    if target.exists():
        raise PublishError(f"receipt_already_exists:{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"status": "pass", "receipt": str(target), **receipt}


def _postgres_schema_version(snapshot: Path) -> int | str:
    path = snapshot / "mvp_v2" / "cloud_runtime.py"
    if not path.is_file():
        return "unavailable"
    match = re.search(
        r"(?m)^POSTGRES_SCHEMA_VERSION\s*=\s*([0-9]+)\s*$",
        path.read_text(encoding="utf-8"),
    )
    return int(match.group(1)) if match else "unavailable"


def main() -> int:
    parser = argparse.ArgumentParser(description="检查或准备 M+ source-only 发布快照")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--source-ref", default="HEAD")
    status = subparsers.add_parser("status")
    status.add_argument("--source-ref", default="HEAD")
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--source-ref", default="HEAD")
    prepare.add_argument("--expected-remote", default="")
    prepare.add_argument("--release-version", default="")
    verify = subparsers.add_parser("verify")
    verify.add_argument("snapshot")
    receipt = subparsers.add_parser("receipt")
    receipt.add_argument("snapshot")
    receipt.add_argument("--output", required=True)
    receipt.add_argument("--package-registry", default="")
    receipt.add_argument("--container-image-digest", default="")
    args = parser.parse_args()
    try:
        if args.command == "check":
            result = check_source(args.source_ref)
        elif args.command == "status":
            result = source_status(args.source_ref)
        elif args.command == "prepare":
            result = prepare_snapshot(
                Path(args.output),
                source_ref=args.source_ref,
                expected_remote=args.expected_remote or None,
                release_version=args.release_version or None,
            )
        elif args.command == "verify":
            result = verify_snapshot(Path(args.snapshot))
        else:
            result = write_release_receipt(
                Path(args.snapshot),
                Path(args.output),
                package_registry=Path(args.package_registry) if args.package_registry else None,
                container_image_digest=args.container_image_digest,
            )
    except (PublishError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"pass", "ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
