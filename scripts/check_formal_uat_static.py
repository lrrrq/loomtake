from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from openpyxl import load_workbook
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]


def check_command(command: list[str]) -> None:
    print("$", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def external_root(raw_path: str) -> Path:
    root = Path(raw_path).expanduser().resolve()
    if root == ROOT or ROOT in root.parents:
        raise SystemExit(f"证据目录必须位于源码仓库外：{root}")
    return root


def verify_delivery(root: Path) -> dict[str, object]:
    database = root / "workspace.sqlite3"
    workbook_path = root / "delivery.xlsx"
    pdf_path = root / "delivery.pdf"
    for path in (database, workbook_path, pdf_path):
        if not path.is_file():
            raise SystemExit(f"交付证据不存在：{path}")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        snapshot = connection.execute(
            """SELECT version_id, image_snapshot_id, shot_count
            FROM delivery_snapshots ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        if snapshot is None:
            raise SystemExit("delivery_snapshots 为空")
        decision_rows = connection.execute(
            """SELECT decision_kind, source_location_json
            FROM version_image_decisions_v3 WHERE snapshot_id=? ORDER BY CAST(shot_no AS INTEGER)""",
            (snapshot["image_snapshot_id"],),
        ).fetchall()
    decisions = len(decision_rows)
    missing_sources = 0
    for row in decision_rows:
        try:
            source = json.loads(row["source_location_json"] or "{}")
            source_row = int(source.get("row") or 0)
        except (TypeError, ValueError, json.JSONDecodeError):
            source, source_row = {}, 0
        if (
            row["decision_kind"] != "image_confirmed"
            or not str(source.get("file") or "").strip()
            or not str(source.get("sheet") or "").strip()
            or source_row < 1
        ):
            missing_sources += 1
    if (
        quick_check != "ok"
        or decisions != int(snapshot["shot_count"])
        or decisions < 1
        or missing_sources != 0
    ):
        raise SystemExit(
            "SQLite evidence invalid: "
            f"quick_check={quick_check}, delivery_shots={snapshot['shot_count']}, "
            f"decisions={decisions}, missing_sources={missing_sources}"
        )
    workbook = load_workbook(workbook_path, read_only=False, data_only=False)
    try:
        image_count = len(workbook["脚本"]._images)
        adjacent_count = workbook["相邻镜头复核"].max_row - 1
        cells = "\n".join(
            str(cell.value or "")
            for sheet in workbook.worksheets
            for row in sheet.iter_rows()
            for cell in row
        )
    finally:
        workbook.close()
    pdf_pages = len(PdfReader(str(pdf_path)).pages)
    if image_count != decisions or adjacent_count != decisions - 1 or pdf_pages != decisions:
        raise SystemExit(
            "Delivery evidence invalid: "
            f"decisions={decisions}, images={image_count}, adjacent={adjacent_count}, pdf={pdf_pages}"
        )
    for header in ("采用来源文件", "采用来源 sheet", "采用来源行"):
        if header not in cells:
            raise SystemExit(f"Delivery source column missing: {header}")
    return {
        "quick_check": quick_check,
        "decisions": decisions,
        "adjacent_reviews": adjacent_count,
        "excel_sha256": sha256(workbook_path),
        "pdf_pages": pdf_pages,
        "pdf_sha256": sha256(pdf_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", default="")
    args = parser.parse_args()
    raw_root = args.evidence_root or __import__("os").environ.get("MPLUS_FORMAL_EVIDENCE_ROOT", "")
    if not raw_root:
        raise SystemExit("必须通过 --evidence-root 或 MPLUS_FORMAL_EVIDENCE_ROOT 指定外部证据目录")
    evidence_root = external_root(raw_root)
    check_command(["node", "--check", "mvp_v2/ui/app.js"])
    check_command(["git", "diff", "--check"])
    check_command([
        sys.executable, "-m", "py_compile",
        "mvp_v2/formal_console.py", "mvp_v2/formal_default_images.py",
        "mvp_v2/formal_generation_service.py", "mvp_v2/formal_image_candidates.py",
        "mvp_v2/formal_image_workspace.py", "mvp_v2/formal_script_revision.py",
        "mvp_v2/loop_runner.py", "mvp_v2/planning_core.py",
    ])
    results: dict[str, object] = {}
    main_browser = evidence_root / "main-browser"
    if main_browser.is_dir():
        results["main-browser"] = verify_delivery(main_browser)
    for journey in ("category-defaults", "text-draft-recovery"):
        root = evidence_root / "zero-images" / journey
        if root.is_dir():
            results[journey] = verify_delivery(root)
    if set(results) != {"main-browser", "category-defaults", "text-draft-recovery"}:
        raise SystemExit(
            "缺少三条完整交付证据：main-browser/category-defaults/text-draft-recovery；"
            f"实际={sorted(results)}"
        )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
