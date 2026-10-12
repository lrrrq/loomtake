from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

SOURCE_ENTRIES = {
    ".git",
    ".gitattributes",
    ".dockerignore",
    ".gitignore",
    ".python-version",
    "AGENTS.md",
    "README.md",
    "deploy",
    "docs",
    "mplus",
    "mvp",
    "mvp_v2",
    "pyproject.toml",
    "requirements-ui.txt",
    "schemas",
    "scripts",
    "tests",
    "uv.lock",
    "各需求文档",
    "方案",
    "启动M+脚本工具.command",
    "启动发布会工具.command",
}

LOCAL_TOOL_ENTRIES = {
    ".DS_Store",
    ".codegraph",
    ".context",
    ".omc",
    ".pytest_cache",
    ".superpowers",
    ".venv",
}

LEGACY_BUSINESS_ENTRIES = {
    "整理后脚本库",
    "网页输出",
}


@dataclass(frozen=True)
class ProjectAreaRecord:
    path: str
    kind: str
    size: int
    mtime_ns: int
    link_target: str = ""


def inventory_project_root(root: str | Path = PROJECT_ROOT) -> list[ProjectAreaRecord]:
    project_root = Path(root).resolve()
    records = []
    for path in sorted(project_root.iterdir(), key=lambda item: item.name):
        stat = path.lstat()
        kind = (
            "symlink"
            if path.is_symlink()
            else "directory"
            if path.is_dir()
            else "file"
        )
        records.append(ProjectAreaRecord(
            path=path.name,
            kind=kind,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            link_target=os.readlink(path) if path.is_symlink() else "",
        ))
    return records


def broken_root_symlinks(root: str | Path = PROJECT_ROOT) -> list[ProjectAreaRecord]:
    return [
        record
        for record in inventory_project_root(root)
        if record.kind == "symlink" and not (Path(root) / record.path).exists()
    ]


def project_root_issues(
    root: str | Path = PROJECT_ROOT,
    *,
    final: bool = False,
) -> list[str]:
    allowed = SOURCE_ENTRIES | LOCAL_TOOL_ENTRIES
    if not final:
        allowed |= LEGACY_BUSINESS_ENTRIES
    issues = []
    for record in inventory_project_root(root):
        if record.path not in allowed:
            issues.append(f"unexpected_root_entry:{record.path}")
        elif record.kind == "symlink" and record.path in SOURCE_ENTRIES:
            issues.append(f"source_entry_must_not_be_symlink:{record.path}")
    if final:
        documents = Path(root) / "方案"
        if documents.is_dir():
            for child in sorted(documents.iterdir(), key=lambda item: item.name):
                if not child.is_file() or child.suffix.casefold() != ".md":
                    issues.append(f"unexpected_plan_entry:{child.name}")
    return issues


def write_records(path: str | Path, records: list[ProjectAreaRecord]) -> Path:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)
    return output


def remove_registered_broken_symlinks(
    root: str | Path,
    records: list[ProjectAreaRecord],
) -> list[str]:
    project_root = Path(root).resolve()
    removed = []
    for record in records:
        path = project_root / record.path
        if (
            record.kind != "symlink"
            or not path.is_symlink()
            or path.exists()
            or os.readlink(path) != record.link_target
        ):
            raise ValueError(f"失效链接已变化，拒绝移除: {record.path}")
        path.unlink()
        removed.append(record.path)
    return removed


def read_records(path: str | Path) -> list[ProjectAreaRecord]:
    rows = []
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(ProjectAreaRecord(**json.loads(line)))
    return rows


def governance_output_dir(data_root: str | Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(data_root).expanduser() / "logs" / "governance" / timestamp
