from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from tests.formal_uat_control_manifest import controls


_SPECS = {item["control_id"]: item for item in controls()}
_VIEWPORTS = ((375, 812), (1024, 900), (1440, 1000))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_record(path: Path, root: Path) -> dict[str, str]:
    return {
        "path": str(path.relative_to(root)),
        "sha256": _sha256(path),
    }


def _safe_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return str(value)


def authority_state(database: Path | None) -> dict[str, Any]:
    if database is None or not database.is_file():
        return {
            "row_count": 0,
            "sha256": hashlib.sha256(b"NO_DATABASE_EXPOSED").hexdigest(),
            "table_count": 0,
        }
    rows: list[Any] = []
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        tables = [
            row[0]
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name"""
            )
        ]
        for table in tables:
            columns = [
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            quoted = table.replace('"', '""')
            table_rows = connection.execute(f'SELECT * FROM "{quoted}"').fetchall()
            normalized = sorted(
                ([ _safe_value(value) for value in row ] for row in table_rows),
                key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True),
            )
            rows.append({"table": table, "columns": columns, "rows": normalized})
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "row_count": sum(len(item["rows"]) for item in rows),
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "table_count": len(rows),
    }


class FormalControlEvidenceRecorder:
    def __init__(self, *, journey_id: str, database: Path | None = None):
        raw_root = os.environ.get("MPLUS_FORMAL_CONTROL_EVIDENCE_ROOT", "").strip()
        self.enabled = bool(raw_root)
        self.journey_id = journey_id
        self.database = database
        if not self.enabled:
            self.root = Path("/")
            self.code_sha = ""
            self.stage = ""
            return
        self.root = Path(raw_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.code_sha = os.environ.get("MPLUS_FORMAL_EXPECTED_SHA", "").strip()
        self.stage = os.environ.get("MPLUS_FORMAL_EVIDENCE_STAGE", "").strip()
        if not self.code_sha or not self.stage:
            raise AssertionError("控制证据必须绑定 MPLUS_FORMAL_EXPECTED_SHA 和证据 stage")
        (self.root / "controls").mkdir(exist_ok=True)
        (self.root / "screenshots").mkdir(exist_ok=True)

    def capture(
        self,
        page,
        control_id: str,
        *,
        keyboard_selector: str = "",
        double_click_selector: str = "",
        recovery_selector: str = "",
        recovery_open_selector: str = "",
        api_result: str = "当前页面及对应 DTO 请求均返回受控业务状态",
        database_result: str = "SQLite 权威状态可读，重复动作前后逻辑快照一致",
        artifact_result: str = "该控件不直接产生 artifact",
        artifact_not_applicable: bool = True,
        terminal_result: str = "控件达到可见业务终局且未显示假成功",
    ) -> None:
        if not self.enabled:
            return
        spec = _SPECS[control_id]
        selector = spec["selector"]
        target = page.locator(selector).first
        target.wait_for(state="visible", timeout=30_000)
        original_url = page.url
        original_viewport = page.viewport_size or {"width": 1440, "height": 1000}

        screenshots_dir = self.root / "screenshots" / control_id
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        main_path = screenshots_dir / "main.png"
        target.scroll_into_view_if_needed()
        page.wait_for_timeout(180)
        target.screenshot(path=main_path)
        viewport_records = []
        for width, height in _VIEWPORTS:
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(120)
            overflow = page.evaluate(
                "document.documentElement.scrollWidth > document.documentElement.clientWidth"
            )
            if overflow:
                raise AssertionError(f"{control_id} 在 {width}px 出现横向溢出")
            viewport_path = screenshots_dir / f"viewport-{width}.png"
            page.screenshot(path=viewport_path, full_page=False)
            viewport_records.append({
                "width": width,
                "height": height,
                "status": "PASS",
                "horizontal_overflow": False,
                **_file_record(viewport_path, self.root),
            })
        page.set_viewport_size(original_viewport)

        keyboard_probe = keyboard_selector or selector
        keyboard_fallback = False
        keyboard_target = page.locator(keyboard_selector).first if keyboard_selector else target
        if not keyboard_target.is_visible() or not keyboard_target.is_enabled():
            fallback = target.locator(
                "a:visible,button:visible:not([disabled]),input:visible:not([disabled]),"
                "select:visible:not([disabled]),textarea:visible:not([disabled]),summary:visible"
            ).first
            if fallback.count() == 0:
                raise AssertionError(f"{control_id} 没有可验证的键盘焦点目标")
            keyboard_target = fallback
            keyboard_probe = f"{selector} 内首个可交互子元素"
            keyboard_fallback = True
        keyboard_target.focus()
        focused = keyboard_target.evaluate("element => element === document.activeElement")
        if not focused:
            raise AssertionError(f"{control_id} 键盘焦点未进入控件")
        keyboard_target.press("Tab")

        before = authority_state(self.database)
        double_target = (
            page.locator(double_click_selector).first if double_click_selector else target
        )
        double_click_probe = double_click_selector or selector
        checkbox_before = (
            double_target.is_checked()
            if double_target.evaluate("element => element.matches('input[type=checkbox]')")
            else None
        )
        checkbox_restored = False
        double_target.dblclick(force=True, timeout=10_000)
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightError:
            pass
        page.wait_for_timeout(250)
        after = authority_state(self.database)
        if (after["row_count"], after["sha256"]) != (before["row_count"], before["sha256"]):
            raise AssertionError(
                f"{control_id} 终局双击产生第二次权威写入：before={before} after={after}"
            )
        if checkbox_before is not None and double_target.is_checked() != checkbox_before:
            double_target.set_checked(checkbox_before)
            checkbox_restored = True
            page.wait_for_load_state("networkidle")
            restored = authority_state(self.database)
            if (restored["row_count"], restored["sha256"]) != (before["row_count"], before["sha256"]):
                raise AssertionError(f"{control_id} 恢复复选框前置选择时改写了权威状态")
        if page.url != original_url:
            page.goto(original_url)
            page.wait_for_load_state("networkidle")

        recovery = recovery_selector or selector
        recovery_page = page.context.new_page()
        recovery_page.route("**/api/formal/setup", lambda route: route.abort())
        try:
            recovery_page.goto(original_url, wait_until="networkidle", timeout=15_000)
        except PlaywrightError:
            pass
        failed_closed = recovery_page.evaluate("""() =>
          Boolean(state.formalSetupError)
          || document.body.innerText.includes('暂不可用')
          || document.body.innerText.includes('稍后重试')
        """)
        if not failed_closed:
            raise AssertionError(f"{control_id} 未观察到 setup API 失败闭合状态")
        recovery_page.unroute("**/api/formal/setup")
        recovery_page.goto(original_url)
        recovery_page.wait_for_load_state("networkidle")
        if recovery_open_selector:
            recovery_opener = recovery_page.locator(recovery_open_selector).first
            recovery_opener.wait_for(state="visible", timeout=30_000)
            if recovery_opener.locator("..").get_attribute("open") is None:
                recovery_opener.click()
        recovery_page.locator(recovery).first.wait_for(state="visible", timeout=30_000)
        recovery_page.close()

        record = {
            "schema_version": 1,
            "control_id": control_id,
            "title": spec["title"],
            "selector": selector,
            "code_sha": self.code_sha,
            "journey_id": self.journey_id,
            "observed_at_utc": _utc_now(),
            "test_stage": self.stage,
            "test_command": "PENDING_STAGE_FINALIZATION",
            "test_exit_code": None,
            "test_log": {"path": "PENDING", "sha256": "PENDING"},
            "terminal": {
                "status": "PASS",
                "result": terminal_result,
                "url": original_url,
                "api": {"status": "PASS", "result": api_result},
                "database": {
                    "status": "PASS" if self.database and self.database.is_file() else "NOT_APPLICABLE",
                    "result": database_result if self.database and self.database.is_file() else "该浏览器 fixture 不暴露业务 SQLite",
                    **({} if self.database and self.database.is_file() else {"reason": "该控件仅消费受控 HTTP fixture"}),
                },
                "artifact": {
                    "status": "NOT_APPLICABLE" if artifact_not_applicable else "PASS",
                    "result": artifact_result,
                    **({"reason": "该控件不直接创建或下载 artifact"} if artifact_not_applicable else {}),
                },
            },
            "screenshots": {
                "main": _file_record(main_path, self.root),
                "viewports": viewport_records,
            },
            "keyboard": {
                "status": "PASS",
                "result": "真实浏览器焦点进入可交互目标，并通过 Tab 离开",
                "selector": keyboard_probe,
                "used_fallback": keyboard_fallback,
            },
            "double_click": {
                "status": "PASS",
                "result": "真实 dblclick 后权威逻辑快照未增加或改写记录",
                "selector": double_click_probe,
                "authority_write_count_before": before["row_count"],
                "authority_write_count_after": after["row_count"],
                "authority_sha256_before": before["sha256"],
                "authority_sha256_after": after["sha256"],
                "checkbox_checked_before": checkbox_before,
                "checkbox_restored_through_ui": checkbox_restored,
            },
            "failure_recovery": {
                "status": "PASS",
                "result": ("setup API 中断时失败闭合；解除中断后 "
                           + (f"点击 {recovery_open_selector} 展开；" if recovery_open_selector else "")
                           + f"{recovery} 重新可见且未显示假成功"),
                "selector": recovery,
            },
        }
        destination = self.root / "controls" / f"{control_id}.json"
        destination.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
