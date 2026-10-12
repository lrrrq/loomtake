"""Isolated H+32 browser fixture server; never a production entry point."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

import uvicorn
from fastapi import FastAPI
from openpyxl import Workbook
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.alpha_brief import AlphaBriefStore
from mvp_v2.alpha_console import AlphaConsole, AlphaRuntime, create_alpha_app
from mvp_v2.alpha_image_flow import AlphaImageFlow
from mvp_v2.alpha_integration import AlphaProjectFlow
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentService, ExecutableAgentServices
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.revision_service import build_revision_report, compare_versions
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceStore


TOKEN = "alpha-browser-fixture-token"


class FixtureBackend:
    def __init__(self, runtime: AlphaRuntime):
        self.runtime = runtime
        self.jobs: dict[str, dict] = {}

    def inspect_action(self, action, parameters, brief_snapshot):
        return {"passed": True, "calls_models": False, "provider_calls": 0, "checks": []}

    def start_action(self, action, parameters, brief_snapshot):
        job_id = f"job_alpha_fixture_{len(self.jobs) + 1:04d}"
        run_id = f"alpha-run-browser-{len(self.jobs) + 1:04d}"
        run_dir = self.runtime.runs_root / "fixture" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        rows, revision = self._script_rows(action, parameters, run_id)
        script = run_dir / "script_rows.json"
        script.write_text(json.dumps(rows, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        report = run_dir / "run_report.json"
        report.write_text(json.dumps({
            "machine_audit_status": "pass", "human_review_status": "pending",
            "blocking_items": [{"shot_no": "1", "code": "fixture_issue", "message": "匿名 fixture 第 1 镜需项目经理处理"}],
        }, ensure_ascii=False), encoding="utf-8")
        artifacts = {
            "run_report": {"path": str(report), "sha256": _sha(report), "size_bytes": report.stat().st_size},
            "workbook": {"path": str(script), "sha256": _sha(script), "size_bytes": script.stat().st_size},
        }
        if revision is not None:
            revision_path = run_dir / "revision_report.json"
            revision_path.write_text(json.dumps(revision, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            artifacts["revision_report"] = {"path": str(revision_path), "sha256": _sha(revision_path), "size_bytes": revision_path.stat().st_size}
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(json.dumps({
            "run_id": run_id, "status": "succeeded", "stages": {"script_plan": {"status": "succeeded", "attempt": 1}},
            "artifacts": artifacts,
        }, ensure_ascii=False), encoding="utf-8")
        self.jobs[job_id] = {
            "job_id": job_id, "state": "complete", "progress": {"stage": "fixture_complete"},
            "run_id": run_id, "parent_run_id": str(parameters.get("parent_run_id") or ""),
            "manifest": manifest, "workbook": script,
        }
        return dict(self.jobs[job_id])

    def _script_rows(self, action, parameters, run_id):
        if action != "fork_revision":
            return [
                {"shot_id": "fixture-shot-1", "镜号": "1", "结构": "开场", "画面描述": "匿名开场", "文案": "匿名产品开场"},
                {"shot_id": "fixture-shot-2", "镜号": "2", "结构": "收束", "画面描述": "匿名收束", "文案": "匿名产品收束"},
            ], None
        parent_run_id = str(parameters.get("parent_run_id") or "")
        parent = self._read_rows(parent_run_id)
        plan = dict(parameters.get("modification_plan") or {})
        match = re.fullmatch(r"把第\s*(\d+)\s*镜文案修改为\s*(.+)", str(plan.get("desired_change") or "").strip())
        if not parent_run_id or match is None:
            raise ValueError("fixture revision 必须使用“把第 N 镜文案修改为 X”的唯一可解析句式")
        shot_no, replacement = match.group(1), match.group(2).strip()
        rows = json.loads(json.dumps(parent, ensure_ascii=False))
        target = next((item for item in rows if str(item.get("镜号")) == shot_no), None)
        if target is None or not replacement:
            raise ValueError("fixture revision 目标镜头或文案无效")
        before = target["文案"]
        target["文案"] = replacement
        diff = compare_versions(
            project_id=str(parameters["project_id"]), parent_version_id=parent_run_id,
            child_version_id=run_id, parent_shots=_revision_shots(parent), child_shots=_revision_shots(rows),
        )
        locks = [
            {"shot_no": str(row["镜号"]), "field": field, "expected_value": value}
            for row in parent for field, value in row.items()
            if field not in {"shot_id", "镜号"} and not (str(row["镜号"]) == shot_no and field == "文案")
        ]
        revision = build_revision_report(
            diff=diff, original_feedback=str(plan["desired_change"]), structured_intent=plan,
            expected_impact={"shot_nos": [shot_no], "fields": ["文案"]}, locked_fields=locks,
            rerun_nodes=["script_plan"], unresolved_items=[],
            machine_review={"status": "pass"}, human_review={"status": "pending"}, publication={"status": "not_created"},
        )
        revision["requested_impact"] = {"shot_no": shot_no, "field": "文案", "before": before, "after": replacement}
        revision["unauthorized_change_count"] = len(revision["actual_impact"]["unexpected_fields"]) + len(revision["actual_impact"]["unexpected_shot_nos"])
        revision["locked_field_violation_count"] = len(revision["locked_field_verification"]["violations"])
        return rows, revision

    def _read_rows(self, run_id):
        path = self.runtime.runs_root / "fixture" / "runs" / run_id / "script_rows.json"
        if not path.is_file():
            raise ValueError("fixture revision 父版脚本 artifact 不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def get_job(self, job_id): return dict(self.jobs[job_id])
    def cancel_job(self, job_id): return {"job_id": job_id, "state": "cancelled", "progress": {}}
    def review_readiness(self, run_id): return {"ready": False, "run_id": run_id, "summary": "fixture 不导出复核包"}
    def prepare_human_review(self, run_id): raise AssertionError("fixture 不导出复核包")
    def finalized_artifacts(self, snapshot):
        job = self.jobs.get(str(snapshot.get("job_id") or ""))
        if not job: return None
        return {
            "run_id": job["run_id"], "parent_run_id": job["parent_run_id"], "manifest_ref": f"artifact://run/{job['run_id']}/manifest",
            "manifest_sha256": _sha(job["manifest"]), "artifact_ref": f"artifact://run/{job['run_id']}/workbook", "content_sha256": _sha(job["workbook"]),
        }


def build_app(root: Path, port: int) -> FastAPI:
    root = root.resolve()
    alpha_store = AlphaBriefStore(root)
    runtime = AlphaRuntime.isolated(root / "runtime", port=port)
    store = WorkspaceStore(runtime.data_root / "workspace.sqlite3")
    identity = LocalIdentityAdapter(store)
    try:
        owner = identity.bootstrap_owner(workspace_name="匿名浏览器工作区", display_name="匿名项目经理", confirmed=True, token=TOKEN)
    except Exception:
        session = identity.resolve_auth_session(TOKEN)
        with store.connection() as connection:
            workspace_id = connection.execute("SELECT workspace_id FROM workspaces ORDER BY created_at LIMIT 1").fetchone()[0]
        owner = {"workspace_id": workspace_id, "actor_id": session["actor_id"], "auth_token": TOKEN}
    _write_five_fixture_inputs(alpha_store.inputs_root)
    readonly = ReadonlyAgentService(ReadonlyAgentServices(
        store=store, identity=identity, projects=ProjectService(store), tasks=TaskService(store),
        versions=VersionService(store), artifacts=RunArtifactResolver(runtime.runs_root),
    ))
    brief_store = BriefArtifactStore(runtime.data_root)
    agent = ExecutableAgentService(ExecutableAgentServices(
        readonly=readonly, projects=readonly.services.projects, tasks=readonly.services.tasks,
        confirmations=ActionConfirmationService(store, b"b" * 32), idempotency=ToolIdempotencyService(store),
        brief_store=brief_store, backend=FixtureBackend(runtime),
    ))
    flow = AlphaProjectFlow(projects=readonly.services.projects, identity=identity, alpha_briefs=alpha_store, normalized_briefs=brief_store)
    images = AlphaImageFlow(projects=readonly.services.projects, identity=identity, alpha_briefs=alpha_store)
    app = create_alpha_app(AlphaConsole(runtime, agent, verified_brief_reader=lambda *_: {"fixture": True}, project_flow=flow, image_flow=images))
    @app.get("/api/alpha/fixture-info")
    def fixture_info():
        return {"fixture": True, "provider_calls": 0, "token": TOKEN, "workspace_id": owner["workspace_id"], "documents": ["brief.pdf", "brand.docx", "facts.xlsx", "events.pptx", "image.png"]}
    return app


def _write_five_fixture_inputs(inputs: Path) -> None:
    inputs.mkdir(exist_ok=True)
    image = inputs / "image.png"
    if not image.exists(): Image.new("RGB", (640, 360), "navy").save(image)
    pdf = inputs / "brief.pdf"
    if not pdf.exists():
        _text_pdf(pdf, "Anonymous fixture source document")
    _office(inputs / "brand.docx", "word/document.xml", "<w:document xmlns:w=\"w\"><w:body><w:p><w:r><w:t>品牌: 匿名品牌</w:t></w:r></w:p></w:body></w:document>")
    xlsx = inputs / "facts.xlsx"
    if not xlsx.exists():
        wb=Workbook(); ws=wb.active; ws.title="资料"
        for row, value in enumerate(["项目名称: 匿名浏览器项目", "脚本类型: 发布会", "目标时长: 60秒", "风格调性: 清晰", "必须出现: 匿名产品演示"], 1): ws.cell(row, 1).value=value
        wb.save(xlsx)
    _office(inputs / "events.pptx", "ppt/slides/slide1.xml", "<p:sld xmlns:p=\"p\"><p:spTree><p:txBody><a:p xmlns:a=\"a\"><a:r><a:t>关键事件: 开场、演示、收束</a:t></a:r></a:p></p:txBody></p:spTree></p:sld>")


def _office(path: Path, entry: str, text: str) -> None:
    if path.exists(): return
    with ZipFile(path, "w", ZIP_DEFLATED) as archive: archive.writestr(entry, text)


def _text_pdf(path: Path, text: str) -> None:
    writer = PdfWriter(); page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = DecodedStreamObject(); content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as handle: writer.write(handle)


def _sha(path: Path) -> str: return hashlib.sha256(path.read_bytes()).hexdigest()


def _revision_shots(rows):
    return [
        {"shot_id": row["shot_id"], "shot_no": row["镜号"], "fields": {key: value for key, value in row.items() if key not in {"shot_id", "镜号"}}}
        for row in rows
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--root", required=True); parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps({"fixture": True, "provider_calls": 0, "token": TOKEN}, ensure_ascii=False), flush=True)
    uvicorn.run(build_app(Path(args.root), args.port), host="127.0.0.1", port=args.port, log_level="warning")
