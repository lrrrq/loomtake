from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_VIEWPORTS = (375, 1024, 1440)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class EvidenceError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _external_root(raw_path: str) -> Path:
    evidence_root = Path(raw_path).expanduser().resolve()
    if evidence_root == ROOT or ROOT in evidence_root.parents:
        raise EvidenceError(
            f"证据目录必须位于源码仓库外，当前为 {evidence_root}"
        )
    return evidence_root


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} 必须是对象")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{label} 必须是非空字符串")
    return value.strip()


def _require_status(
    value: Any,
    label: str,
    *,
    allow_not_applicable: bool = False,
) -> dict[str, Any]:
    record = _require_mapping(value, label)
    allowed = {"PASS", "NOT_APPLICABLE"} if allow_not_applicable else {"PASS"}
    if record.get("status") not in allowed:
        raise EvidenceError(
            f"{label}.status 必须为 {'/'.join(sorted(allowed))}，实际为 {record.get('status')!r}"
        )
    _require_text(record.get("result"), f"{label}.result")
    if record.get("status") == "NOT_APPLICABLE":
        _require_text(record.get("reason"), f"{label}.reason")
    return record


def _evidence_file(root: Path, raw_path: Any, label: str) -> Path:
    root = root.resolve()
    relative = Path(_require_text(raw_path, label))
    if relative.is_absolute() or ".." in relative.parts:
        raise EvidenceError(f"{label} 必须是证据根目录内的相对路径")
    path = (root / relative).resolve()
    if root not in path.parents:
        raise EvidenceError(f"{label} 越出证据根目录")
    if not path.is_file():
        raise EvidenceError(f"{label} 文件不存在：{path}")
    return path


def _check_file_record(root: Path, value: Any, label: str) -> Path:
    record = _require_mapping(value, label)
    path = _evidence_file(root, record.get("path"), f"{label}.path")
    expected = _require_text(record.get("sha256"), f"{label}.sha256")
    actual = _sha256(path)
    if actual != expected:
        raise EvidenceError(
            f"{label} SHA-256 不一致：expected={expected} actual={actual}"
        )
    return path


def validate_control_record(
    record: dict[str, Any],
    *,
    spec: dict[str, Any],
    evidence_root: Path,
    expected_sha: str,
) -> dict[str, Any]:
    control_id = spec["control_id"]
    if record.get("schema_version") != 1:
        raise EvidenceError(f"{control_id}.schema_version 必须为 1")
    for field in ("control_id", "title", "selector"):
        if record.get(field) != spec[field]:
            raise EvidenceError(
                f"{control_id}.{field} 与 manifest 不一致：{record.get(field)!r}"
            )
    if record.get("code_sha") != expected_sha:
        raise EvidenceError(
            f"{control_id}.code_sha 未绑定待验收提交：{record.get('code_sha')!r}"
        )
    _require_text(record.get("journey_id"), f"{control_id}.journey_id")
    _require_text(record.get("observed_at_utc"), f"{control_id}.observed_at_utc")
    _require_text(record.get("test_command"), f"{control_id}.test_command")
    if record.get("test_exit_code") != 0:
        raise EvidenceError(f"{control_id}.test_exit_code 必须为 0")
    _check_file_record(evidence_root, record.get("test_log"), f"{control_id}.test_log")

    terminal = _require_status(record.get("terminal"), f"{control_id}.terminal")
    _require_text(terminal.get("url"), f"{control_id}.terminal.url")
    _require_status(
        terminal.get("api"), f"{control_id}.terminal.api", allow_not_applicable=True
    )
    _require_status(
        terminal.get("database"),
        f"{control_id}.terminal.database",
        allow_not_applicable=True,
    )
    _require_status(
        terminal.get("artifact"),
        f"{control_id}.terminal.artifact",
        allow_not_applicable=True,
    )

    screenshots = _require_mapping(record.get("screenshots"), f"{control_id}.screenshots")
    _check_file_record(evidence_root, screenshots.get("main"), f"{control_id}.screenshots.main")
    viewport_rows = screenshots.get("viewports")
    if not isinstance(viewport_rows, list):
        raise EvidenceError(f"{control_id}.screenshots.viewports 必须是数组")
    widths = [row.get("width") for row in viewport_rows if isinstance(row, dict)]
    if widths != list(REQUIRED_VIEWPORTS):
        raise EvidenceError(
            f"{control_id} viewport 必须严格为 {list(REQUIRED_VIEWPORTS)}，实际为 {widths}"
        )
    for viewport in viewport_rows:
        width = viewport["width"]
        if viewport.get("status") != "PASS":
            raise EvidenceError(f"{control_id}.viewport[{width}] 未 PASS")
        if viewport.get("horizontal_overflow") is not False:
            raise EvidenceError(f"{control_id}.viewport[{width}] 存在横向溢出或未核验")
        _check_file_record(
            evidence_root,
            viewport,
            f"{control_id}.screenshots.viewport[{width}]",
        )

    keyboard = _require_status(record.get("keyboard"), f"{control_id}.keyboard")
    _require_text(keyboard.get("selector"), f"{control_id}.keyboard.selector")
    if not isinstance(keyboard.get("used_fallback"), bool):
        raise EvidenceError(f"{control_id}.keyboard.used_fallback 必须为布尔值")
    double_click = _require_status(record.get("double_click"), f"{control_id}.double_click")
    _require_text(double_click.get("selector"), f"{control_id}.double_click.selector")
    before = double_click.get("authority_write_count_before")
    after = double_click.get("authority_write_count_after")
    if not isinstance(before, int) or not isinstance(after, int) or after != before:
        raise EvidenceError(
            f"{control_id}.double_click 必须证明第二次操作未增加权威写入：before={before!r} after={after!r}"
        )
    before_sha = _require_text(
        double_click.get("authority_sha256_before"),
        f"{control_id}.double_click.authority_sha256_before",
    )
    after_sha = _require_text(
        double_click.get("authority_sha256_after"),
        f"{control_id}.double_click.authority_sha256_after",
    )
    if before_sha != after_sha:
        raise EvidenceError(f"{control_id}.double_click 前后权威逻辑快照不同")
    failure_recovery = _require_status(
        record.get("failure_recovery"), f"{control_id}.failure_recovery"
    )
    _require_text(
        failure_recovery.get("selector"),
        f"{control_id}.failure_recovery.selector",
    )
    return {
        "control_id": control_id,
        "journey_id": record["journey_id"],
        "main_screenshot": screenshots["main"]["path"],
    }


def audit(evidence_root: Path, expected_sha: str) -> list[dict[str, Any]]:
    from tests.formal_uat_control_manifest import controls

    if not evidence_root.is_dir():
        raise EvidenceError(f"证据目录不存在：{evidence_root}")
    validated = []
    for spec in controls():
        control_id = spec["control_id"]
        record_path = evidence_root / "controls" / f"{control_id}.json"
        if not record_path.is_file():
            raise EvidenceError(f"缺少 {control_id} 独立终局记录：{record_path}")
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise EvidenceError(f"{control_id} 记录不可读：{error}") from error
        validated.append(validate_control_record(
            _require_mapping(record, control_id),
            spec=spec,
            evidence_root=evidence_root,
            expected_sha=expected_sha,
        ))
    return validated


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed audit for M01-M60 independent browser evidence",
    )
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--expected-sha", default="")
    args = parser.parse_args()
    try:
        evidence_root = _external_root(args.evidence_root)
        expected_sha = args.expected_sha.strip() or _git_head()
        validated = audit(evidence_root, expected_sha)
    except (EvidenceError, subprocess.CalledProcessError) as error:
        print(f"INVALID: {error}", file=sys.stderr)
        return 1
    summary = {
        "status": "PASS",
        "code_sha": expected_sha,
        "control_count": len(validated),
        "controls": validated,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
