from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


COMMANDS = {
    "static": ["uv", "run", "--locked", "--python", "3.10", "python", "scripts/check_formal_uat_static.py"],
    "focused": [
        "uv", "run", "--locked", "--python", "3.10", "python", "-m", "unittest", "-f",
        "tests.test_mvp_v2_planning_core",
        "tests.test_mvp_v2_formal_generation_service",
        "tests.test_mvp_v2_formal_console",
        "tests.test_mvp_v2_formal_script_workspace",
        "tests.test_mvp_v2_formal_image_workspace",
        "tests.test_mvp_v2_formal_capability_regression",
        "tests.test_mvp_v2_agent_tools",
        "tests.test_mvp_v2_formal_uat_readiness",
        "tests.test_mvp_v2_formal_revision_union",
        "tests.test_mvp_v2_formal_control_evidence_audit",
    ],
    "browser": [
        "uv", "run", "--locked", "--python", "3.10", "python", "-m", "unittest", "-f",
        "tests.test_mvp_v2_formal_browser_journey",
    ],
    "zero-images": [
        "uv", "run", "--locked", "--python", "3.10", "python", "-m", "unittest", "-f",
        "tests.test_mvp_v2_formal_zero_image_browser_journeys",
    ],
    "controls": [
        "uv", "run", "--locked", "--python", "3.10", "python", "-m", "unittest", "-f",
        "tests.test_mvp_v2_formal_control_browser_evidence",
        "tests.test_mvp_v2_ui00_journey3.Ui00JourneyThreeTests.test_hard_template_conflict_requires_two_step_manual_confirmation",
    ],
    "full": [
        "uv", "run", "--locked", "--python", "3.10", "python", "-m", "unittest",
        "discover", "-s", "tests", "-f",
    ],
    "pytest": [
        "uv", "run", "--locked", "--python", "3.10", "python", "-m", "pytest", "-x", "-q",
    ],
    "doctor": ["./mplus", "doctor"],
}


REQUIRED_STAGES = tuple(COMMANDS)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_summary(
    evidence_root: Path,
    summary: dict[str, object],
    *,
    code_sha: str,
) -> None:
    stage_rows = {
        stage: summary.get(stage)
        for stage in REQUIRED_STAGES
        if isinstance(summary.get(stage), dict)
    }
    missing = [stage for stage in REQUIRED_STAGES if stage not in stage_rows]
    invalid = [
        stage for stage, row in stage_rows.items()
        if not row.get("valid") or row.get("code_sha") != code_sha
    ]
    control_count = len(list((evidence_root / "controls").glob("M??.json")))
    approved = not missing and not invalid and control_count == 60
    summary["_readiness"] = {
        "schema_version": "mplus-formal-uat-readiness/v1",
        "generated_at_utc": utc_now(),
        "code_sha": code_sha,
        "machine_uat_readiness": "APPROVE" if approved else "REVISE",
        "production_certification": "BLOCK",
        "human_uat": "pending_review",
        "stage_count": len(stage_rows),
        "control_count": control_count,
        "missing_stages": missing,
        "invalid_stages": invalid,
        "reason": (
            "全部机器阶段与 60 项控件证据通过；真人质量、部署和生产认证仍是独立门禁。"
            if approved
            else "机器证据尚未完整或存在非零阶段，不得更新主分支。"
        ),
    }
    (evidence_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def external_root(raw_path: str) -> Path:
    root = Path(raw_path).expanduser().resolve()
    if root == ROOT or ROOT in root.parents:
        raise SystemExit(f"证据目录必须位于源码仓库外：{root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def stage_environment(evidence_root: Path, stage: str, code_sha: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "MPLUS_FORMAL_EVIDENCE_ROOT": str(evidence_root),
        "MPLUS_FORMAL_EXPECTED_SHA": code_sha,
    })
    if stage == "browser":
        root = evidence_root / "main-browser"
        root.mkdir(parents=True, exist_ok=True)
        environment.update({
            "MPLUS_FORMAL_MAIN_BROWSER_ROOT": str(root),
            "MPLUS_FORMAL_DATABASE_PATH": str(root / "workspace.sqlite3"),
            "MPLUS_FORMAL_SERVER_LOG_PATH": str(root / "server.log"),
            "MPLUS_FORMAL_SCREENSHOT_PATH": str(root / "terminal.png"),
            "MPLUS_FORMAL_TASKS_SCREENSHOT_PATH": str(root / "tasks.png"),
            "MPLUS_FORMAL_SOURCE_WORKBOOK_PATH": str(root / "source.xlsx"),
            "MPLUS_FORMAL_EXCEL_PATH": str(root / "delivery.xlsx"),
            "MPLUS_FORMAL_PDF_PATH": str(root / "delivery.pdf"),
        })
    if stage == "zero-images":
        root = evidence_root / "zero-images"
        root.mkdir(parents=True, exist_ok=True)
        environment["MPLUS_ZERO_IMAGE_EVIDENCE_ROOT"] = str(root)
    if stage in {"browser", "zero-images", "controls"}:
        environment["MPLUS_FORMAL_CONTROL_EVIDENCE_ROOT"] = str(evidence_root)
        environment["MPLUS_FORMAL_EVIDENCE_STAGE"] = stage
    if stage == "controls":
        root = evidence_root / "controls-run"
        root.mkdir(parents=True, exist_ok=True)
        environment["MPLUS_FORMAL_CONTROL_LEGACY_ROOT"] = str(root)
    return environment


def finalize_control_records(
    evidence_root: Path,
    stage: str,
    result: dict[str, object],
) -> None:
    controls_root = evidence_root / "controls"
    if not controls_root.is_dir():
        return
    for path in controls_root.glob("M??.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("test_stage") != stage:
            continue
        record["test_command"] = result["command"]
        record["test_exit_code"] = result["exit_code"]
        record["test_log"] = {
            "path": result["log"],
            "sha256": result["log_sha256"],
        }
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def run_stage(evidence_root: Path, stage: str, code_sha: str) -> dict[str, object]:
    stage_root = evidence_root / "stages"
    stage_root.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    log_path = stage_root / f"{stage}.log"
    command = COMMANDS[stage]
    environment = stage_environment(evidence_root, stage, code_sha)
    with log_path.open("wb") as log:
        log.write((json.dumps({
            "stage": stage,
            "started_at_utc": started,
            "code_sha": code_sha,
            "command": shlex.join(command),
        }, ensure_ascii=False) + "\n").encode("utf-8"))
        process = subprocess.run(
            command, cwd=ROOT, env=environment,
            stdout=log, stderr=subprocess.STDOUT, check=False,
        )
        finished = utc_now()
        log.write((json.dumps({
            "finished_at_utc": finished,
            "exit_code": process.returncode,
        }, ensure_ascii=False) + "\n").encode("utf-8"))
    result: dict[str, object] = {
        "stage": stage,
        "started_at_utc": started,
        "finished_at_utc": finished,
        "code_sha": code_sha,
        "command": shlex.join(command),
        "exit_code": process.returncode,
        "log": str(log_path.relative_to(evidence_root)),
        "log_sha256": digest(log_path),
        "valid": process.returncode == 0,
    }
    summary_path = evidence_root / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    summary[stage] = result
    write_summary(evidence_root, summary, code_sha=code_sha)
    return result


def audit_controls(evidence_root: Path, code_sha: str) -> int:
    command = [
        sys.executable, "scripts/audit_formal_uat_controls.py",
        "--evidence-root", str(evidence_root), "--expected-sha", code_sha,
    ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=tuple(COMMANDS))
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--expected-sha", default="")
    args = parser.parse_args()
    evidence_root = external_root(args.evidence_root)
    code_sha = args.expected_sha.strip() or git_head()
    if code_sha != git_head():
        raise SystemExit(
            f"拒绝在非当前 HEAD 上生成证据：expected={code_sha} HEAD={git_head()}"
        )
    result = run_stage(evidence_root, args.stage, code_sha)
    if result["exit_code"] != 0:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(result["exit_code"])
    finalize_control_records(evidence_root, args.stage, result)
    if args.stage == "controls":
        audit_exit = audit_controls(evidence_root, code_sha)
        result["controls_audit_exit_code"] = audit_exit
        result["valid"] = result["exit_code"] == 0 and audit_exit == 0
        summary_path = evidence_root / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary[args.stage] = result
        write_summary(evidence_root, summary, code_sha=code_sha)
        if audit_exit != 0:
            return audit_exit
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
