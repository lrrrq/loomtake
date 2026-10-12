from __future__ import annotations

from pathlib import Path


def resolve_run_local_path(
    run_dir: str | Path,
    recorded_path: str | Path,
) -> Path:
    """Resolve a run-local path after the whole data root has moved.

    Historical manifests stored absolute paths.  Relocation is allowed only
    when the recorded path contains the same run directory name and the
    resulting suffix stays inside the configured run directory.  Callers must
    still verify the expected filename and SHA-256.
    """
    root = Path(run_dir).expanduser().resolve()
    raw = Path(str(recorded_path or "")).expanduser()
    if not str(raw):
        return raw
    if not raw.is_absolute():
        return (root / raw).resolve()

    direct = raw.resolve()
    if _is_within(direct, root):
        return direct

    indices = [index for index, part in enumerate(raw.parts) if part == root.name]
    for index in reversed(indices):
        suffix = raw.parts[index + 1 :]
        if not suffix or any(part in {"", ".", ".."} for part in suffix):
            continue
        relocated = root.joinpath(*suffix).resolve()
        if _is_within(relocated, root):
            return relocated
    return direct


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
