from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import fcntl


def generate_run_id() -> str:
    return uuid.uuid4().hex


def is_lock_held(path: str | Path) -> bool:
    """Observe the OS lock, so stale files after a restart do not imply execution."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@contextmanager
def try_exclusive_lock(path: str | Path, run_id: str | None = None):
    lock = exclusive_lock(path, run_id=run_id)
    try:
        lock.__enter__()
    except RuntimeError as exc:
        if isinstance(exc.__cause__, BlockingIOError):
            yield False
            return
        raise
    try:
        yield True
    finally:
        lock.__exit__(None, None, None)


def create_run_dir(root: str | Path, run_id: str) -> Path:
    path = Path(root) / run_id
    path.mkdir(parents=True, exist_ok=False)
    return path


@contextmanager
def exclusive_lock(path: str | Path, run_id: str | None = None):
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_start = _process_start(os.getpid())
    metadata = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "process_start": process_start or f"unknown:{os.getpid()}",
        "created_at": _utc_now(),
        "run_id": run_id or generate_run_id(),
    }
    serialized = (json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd, existed = _open_lock_file(lock_path, serialized)
    owns_lock = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"锁已被占用: {lock_path}") from exc

        previous = _read_lock_metadata(fd) if existed else None
        if existed:
            _validate_recoverable_lock(previous, lock_path)

        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, serialized)
        os.fsync(fd)
        owns_lock = True
        yield lock_path
    finally:
        try:
            if owns_lock and lock_path.exists() and lock_path.stat().st_ino == os.fstat(fd).st_ino:
                lock_path.unlink()
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _open_lock_file(lock_path: Path, serialized: bytes) -> tuple[int, bool]:
    while True:
        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".{lock_path.name}.",
            suffix=".tmp",
            dir=lock_path.parent,
        )
        temp_path = Path(temp_name)
        try:
            os.write(temp_fd, serialized)
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = -1
            try:
                os.link(temp_path, lock_path)
                existed = False
            except FileExistsError:
                existed = True
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        try:
            return os.open(lock_path, os.O_RDWR), existed
        except FileNotFoundError:
            continue


def _read_lock_metadata(fd: int) -> dict | None:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 64 * 1024).decode("utf-8").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _validate_recoverable_lock(metadata: dict | None, lock_path: Path) -> None:
    if not isinstance(metadata, dict) or not _valid_lock_metadata(metadata):
        raise RuntimeError(f"锁元数据无效，拒绝抢占: {lock_path}")
    if metadata.get("host") != socket.gethostname():
        raise RuntimeError(f"远端主机锁不可抢占: {lock_path}")
    pid = metadata.get("pid")
    process_start = metadata.get("process_start")
    current_start = _process_start(pid)
    if current_start == process_start:
        raise RuntimeError(f"锁已被占用: {lock_path}")
    if current_start is None and _pid_is_alive(pid):
        raise RuntimeError(f"锁已被占用: {lock_path}")
    if str(process_start).startswith("unknown:") and _pid_is_alive(pid):
        raise RuntimeError(f"锁已被占用: {lock_path}")


def _valid_lock_metadata(metadata: dict) -> bool:
    if type(metadata.get("pid")) is not int or metadata["pid"] <= 0:
        return False
    for field in ("host", "process_start", "run_id", "created_at"):
        value = metadata.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    timestamp = metadata["created_at"]
    if timestamp.endswith("Z"):
        timestamp = timestamp[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _process_start(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value or None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json_atomic(path: str | Path, payload: dict | list) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def append_jsonl_ledger(
    path: str | Path,
    run_id: str,
    event: str,
    details: dict | None = None,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if details is not None:
        record["details"] = details
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def cache_fingerprint(schema, rule, prompt, provider) -> str:
    payload = {
        "schema": schema,
        "rule": rule,
        "prompt": prompt,
        "provider": provider,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
