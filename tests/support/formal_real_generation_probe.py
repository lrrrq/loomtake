"""Opt-in anonymous XLSX -> real image analysis -> Formal generation evidence.

Uses the real fixture mode, which composes build_formal_runtime unchanged.
Never supplies manual image analyses, approves quality, or publishes a version.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from mvp_v2.formal_brief_input_adapter import UploadedDocument
from mvp_v2.text_providers import _redact_diagnostics, _transport_evidence


def dump(path: Path, value) -> None:
    path.write_text(json.dumps(_redact_diagnostics(value), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    path.chmod(0o600)


def make_brief(fixture) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Brief"
    values = (
        "项目名称: 匿名新品体验会", "脚本类型: 发布会", "品牌或客户: 匿名品牌",
        "视频目标: 清楚记录嘉宾说明与产品演示", "使用场景: 室内发布会现场", "目标时长: 60秒",
        "风格调性: 克制、真实、节奏连贯", "必须出现的内容: 产品演示、说明发布重点",
        "关键事件与流程: 嘉宾发布、说明发布重点、产品演示、品牌收束",
        "禁止或不能出现的内容: 不得虚构未确认的人名、城市、数量、产品功能或活动结果",
        "特殊需求: 配乐连贯推进，允许明确的声音设计建议，未确认事实保留待确认",
        "交付要求: 输出10镜脚本；图片仅为创意参考，无合适图片时允许留空；未要求旁白或字幕时可留空",
    )
    for value in values:
        sheet.append([value])
    # Distinct anonymous images already tracked in the visual fixture atlas:
    # product demonstration, speaker, abstract closing visual.
    for cell, name in zip(("C2", "C14", "C26"), ("11-reference.png", "16-reference.png", "22-reference.png")):
        image = ExcelImage(str(fixture.fixture_dir / name))
        image.width, image.height = 320, 180
        sheet.add_image(image, cell)
    path = fixture.root / "anonymous-brief.xlsx"
    workbook.save(path)
    workbook.close()
    return path


def observe_provider(root: Path, stage: str, invoke):
    """Observe the actual production callable; forward its arguments unchanged."""
    def observed(prompt, model, max_tokens):
        entry = {"stage": stage, "scope": "production_callable_input", "requested_model": model,
            "requested_max_tokens": max_tokens, "prompt_sha256": sha256(prompt.encode()).hexdigest(),
            "started_at": datetime.now(timezone.utc).isoformat()}
        started = time.monotonic()
        try:
            result = invoke(prompt, model, max_tokens)
            entry.update(status="succeeded", transport=_transport_evidence(result))
            return result
        except Exception as exc:
            entry.update(status="failed", error_type=type(exc).__name__, transport=_transport_evidence(exc))
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - started, 3)
            path = root / "private-provider-ledger.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(_redact_diagnostics(entry), ensure_ascii=False) + "\n")
            path.chmod(0o600)
            print(json.dumps({"stage": stage, "status": entry["status"], "seconds": entry["seconds"]}), flush=True)
    return observed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-real-provider", action="store_true", required=True)
    parser.add_argument("--resume", "--resume-brief-confirmation", dest="resume", action="store_true",
        help="Resume a confirmed/frozen Brief after a failure before image analysis; no repeated model import.")
    parser.add_argument("--supplement-image", type=Path,
        help="Add a new actual anonymous image through the existing upload authority before resuming analysis.")
    parser.add_argument("--generation-mode", choices=("image_backed", "text_only_draft"), default="image_backed",
        help="Use the explicit production generation mode; text-only leaves unapproved images unavailable.")
    parser.add_argument("--resume-planning", action="store_true",
        help="Replan the same confirmed Brief after a diagnosed planning fix; retain old plan evidence.")
    parser.add_argument("--retry-generation", action="store_true",
        help="Use the existing authenticated stage-retry authority on the failed generation and frozen plan.")
    args = parser.parse_args()
    root = args.output.expanduser().resolve()
    previous = None
    if args.resume or args.resume_planning or args.retry_generation:
        previous = json.loads((root / "real-journey.json").read_text(encoding="utf-8"))
        stages = {"brief_confirm", "image_understanding"} | ({"plan"} if args.supplement_image or args.generation_mode == "text_only_draft" else set())
        if args.resume_planning and previous.get("error", {}).get("code") == "PLANNING_NEEDS_CLARIFICATION":
            stages.add("generate")
        if args.retry_generation and previous.get("error", {}).get("code") == "FORMAL_GENERATION_AUDIT_FAILED":
            stages.add("generate")
        if previous.get("status") != "blocked" or previous.get("failure_stage") not in stages:
            raise SystemExit("This resume option only accepts a failure before image analysis has completed.")
        suffix = "stage-retry" if args.retry_generation else "planning-fix" if args.resume_planning else "text-only" if args.generation_mode == "text_only_draft" else "resume"
        previous_path = root / f"real-journey-before-{previous['failure_stage']}-{suffix}.json"
        if previous_path.exists():
            raise SystemExit("A resume for this stage was already attempted; preserve its evidence.")
        dump(previous_path, previous)
        if args.resume_planning:
            old_plan = root / "plan-preview.json"
            destination = root / "plan-preview-before-planning-fix.json"
            if destination.exists():
                raise SystemExit("A planning-fix resume already exists; preserve its evidence.")
            old_plan.rename(destination)
    elif root.exists() and any(root.iterdir()):
        raise SystemExit("Use a fresh empty output directory; previous provider evidence is never overwritten.")
    os.environ.update(MPLUS_FORMAL_FIXTURE_ROOT=str(root), MPLUS_FORMAL_FIXTURE_MODE="real",
        MPLUS_FORMAL_FIXTURE_SCENARIO="default")
    from tests.support import formal_console_fixture_server as fixture
    root.chmod(0o700)
    record = {"started_at": datetime.now(timezone.utc).isoformat(), "mode": fixture.FIXTURE_MODE,
        "generation_mode": args.generation_mode,
        "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "source_sha256": {name: sha256((REPO / name).read_bytes()).hexdigest() for name in (
            "mvp_v2/formal_runtime.py", "mvp_v2/script_draft.py", "mvp_v2/text_providers.py",
            "mvp_v2/image_understanding.py", "mvp_v2/planning_core.py", "mvp_v2/brand_policy.py",
            "mvp_v2/formal_generation_service.py", "mvp_v2/formal_admin_service.py",
            "tests/support/formal_console_fixture_server.py", str(Path(__file__).relative_to(REPO)))},
        "manual_image_analysis_count": 0, "production_quality_acceptance": "UNPROVEN", "stages": []}
    provider_ledger = root / "private-provider-ledger.jsonl"
    previous_provider_calls = len(provider_ledger.read_text().splitlines()) if provider_ledger.exists() else 0
    dump(root / "real-journey.json", record)
    stage = "preflight"
    try:
        generation = fixture.generation
        if generation.provider_policy is None or generation.vision_provider_policy is None or fixture.adapter.understanding_policy is None:
            raise RuntimeError("Configured production text or vision capability unavailable; no replacement provider.")
        generation.provider_policy = replace(generation.provider_policy,
            writer=observe_provider(root, "writer", generation.provider_policy.writer),
            editor=observe_provider(root, "editor", generation.provider_policy.editor))
        fixture.adapter.understanding_policy = replace(fixture.adapter.understanding_policy,
            invoke=observe_provider(root, "brief_understanding", fixture.adapter.understanding_policy.invoke))
        owner = (fixture.identity.reissue_local_owner_session(confirmed=True) if previous else
            fixture.identity.bootstrap_owner(workspace_name="匿名真实模型验收", display_name="匿名验收人", confirmed=True))
        token, workspace = owner["auth_token"], owner["workspace_id"]
        project = (fixture.projects.get_project(token, previous["project_id"]) if previous else
            fixture.projects.create_project(token, workspace_id=workspace, name="匿名新品体验会", category_id="launch", expected_workspace_version=0))
        project_id = project["project_id"]
        scope = {"workspace_id": workspace, "project_id": project_id}
        def version():
            return fixture.projects.get_project(token, project_id)["project_version"]
        input_path = Path(previous["input_file"]) if previous else make_brief(fixture)
        record.update(input_file=str(input_path), input_sha256=sha256(input_path.read_bytes()).hexdigest(), project_id=project_id)
        stage = "brief_import"
        if previous:
            imported = json.loads((root / "brief-import.json").read_text(encoding="utf-8"))
            record["resumed_frozen_brief"] = True
        else:
            imported = fixture.adapter.import_uploads(token, **scope, expected_project_version=version(),
                uploads=[UploadedDocument(input_path.name, BytesIO(input_path.read_bytes()), critical=True)])
            dump(root / "brief-import.json", imported)
        if imported.get("has_failed_extractions") or imported.get("unresolved_conflict_count"):
            raise RuntimeError("Brief extraction or conflict needs resolution; evidence retained without forced confirmation.")
        stage = "brief_confirm"
        if previous and (root / "brief-confirmed.json").is_file():
            confirmed = json.loads((root / "brief-confirmed.json").read_text(encoding="utf-8"))
        else:
            confirmed = fixture.adapter.confirm(token, **scope, brief_version_id=imported["brief_key"],
                confirmed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), expected_project_version=version())
            dump(root / "brief-confirmed.json", confirmed)
        stage = "image_understanding"
        if args.supplement_image:
            source = args.supplement_image.expanduser().resolve()
            content = source.read_bytes()
            uploaded = fixture.image_workspace.supplement_local_image(token, **scope,
                expected_project_version=version(), filename=source.name, content=content)
            dump(root / "supplement-upload.json", {"source": str(source), "sha256": sha256(content).hexdigest(), **uploaded})
            if (root / "image-readiness.json").is_file():
                dump(root / "image-readiness-before-supplement.json",
                    json.loads((root / "image-readiness.json").read_text(encoding="utf-8")))
        readiness = generation.analyze_pending_images(token, **scope, expected_project_version=version())
        dump(root / "image-readiness.json", readiness)
        record["image_readiness"] = readiness
        if args.retry_generation:
            stage = "generation_stage_retry"
            frozen_plan = json.loads((root / "plan-preview.json").read_text())
            record["reused_plan_id"] = frozen_plan["plan_id"]
            operations = fixture.formal_runtime_bundle.admin
            proposed = operations.propose_stage_retry(token, **scope,
                requested_stage_id="formal_generation_plan", reason="品牌抽取误报已修复，按原确认规划重试生成。",
                expected_project_version=version(), idempotency_key="real-anonymous-brand-audit-retry")
            dump(root / "generation-retry-proposal.json", proposed)
            result = operations.confirm_stage_retry(token, workspace_id=workspace,
                proposal_id=proposed["proposal_id"], proposal_digest=proposed["proposal_digest"])
            dump(root / "generation-retry-result.json", result)
            if result.get("execution", {}).get("terminal_status") != "succeeded":
                raise RuntimeError("Canonical generation retry did not succeed; inspect its saved terminal evidence.")
        else:
            stage = "plan"
            plan = generation.prepare_plan(token, **scope, expected_project_version=version(), generation_mode=args.generation_mode)
            dump(root / "plan-preview.json", plan)
            stage = "generate"
            result = generation.confirm_plan_and_generate(token, **scope, plan_id=plan["plan_id"], expected_project_version=version())
        dump(root / "generation-result.json", result)
        stage = "review_workbook"
        response = fixture.script_workspace.build_review_workbook(token, **scope, version_no=1)
        review_path = root / response["filename"]
        review_path.write_bytes(response["content"])
        workbook = load_workbook(review_path)
        record["review_workbook"] = {"path": str(review_path), "sha256": sha256(review_path.read_bytes()).hexdigest(),
            "sheets": [{"name": sheet.title, "rows": sheet.max_row, "columns": sheet.max_column, "images": len(sheet._images)} for sheet in workbook]}
        workbook.close()
        record.update(status="completed", generation=result, version_count=len(fixture.projects.list_project_versions(token, project_id)))
    except Exception as exc:
        record.update(status="blocked", failure_stage=stage, error={"type": type(exc).__name__, "code": getattr(exc, "code", None), "message": str(exc)},
            diagnostics=getattr(exc, "diagnostics", {}))
    finally:
        ledger_rows = [json.loads(line) for line in provider_ledger.read_text().splitlines()] if provider_ledger.exists() else []
        record["provider_calls_this_invocation"] = ledger_rows[previous_provider_calls:]
        record["actual_provider_calls_total"] = len(ledger_rows)
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        dump(root / "real-journey.json", record)
        print(json.dumps({key: record[key] for key in ("status", "failure_stage", "review_workbook") if key in record}, ensure_ascii=False), flush=True)
    return 0 if record["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
