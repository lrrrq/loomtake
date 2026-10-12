"""The handshake's creation half, using real services and anonymous text providers."""
from io import BytesIO
from pathlib import Path
import json
import tempfile
import unittest

from openpyxl import load_workbook

from mvp_v2.ai_spec import refresh_ai_spec
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.creative_generation import repository
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_generation_service import FormalGenerationService, FormalGenerationServices, FormalTextProviderPolicy
from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.knowledge_service import KnowledgeService
from mvp_v2.package_publication import AssetPackagePublicationService
from mvp_v2.package_registry import LocalPackageRegistry
from mvp_v2.project_image_authority import ProjectImagePoolService
from mvp_v2.project_service import ProjectService
from mvp_v2.run_contract import sha256_file
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceStore, utc_now
from tests.test_mvp_v2 import write_launch_library
from tests import test_mvp_v2_formal_generation_service as formal_tests


SECTIONS = ["氛围造境", "嘉年华互动", "颁奖典礼", "派对狂欢", "派对狂欢"]
VISUALS = [
    "年会现场入口和主题空间依次入画，参加者在入口交流",
    "年会互动区的人群体验活动，近景记录交流与回应",
    "年会颁奖典礼现场呈现登台动作与观众鼓掌的呼应",
    "年会派对现场以人群互动和舞台氛围推进情绪",
    "年会现场的集体合影与会场空间构成结尾，呈现团队凝聚力",
]


def write_annual_template(path):
    rows = []
    for number in range(1, 26):
        section = "氛围造境" if number <= 4 else "嘉年华互动" if number <= 10 else "颁奖典礼" if number <= 19 else "派对狂欢"
        rows.append([section, number, None, None, "", "摄影", "全景", "固定",
            f"年会{section}现场的参与者与空间关系", "现场氛围与音乐推进", ""])
    write_launch_library(path, rows, [])
    wb = load_workbook(path)
    try:
        for name in ("模版", "创意库"):
            ws = wb[name]
            ws["A1"] = "匿名年会25镜模板" if name == "模版" else "匿名年会创意库"
            for col, title in enumerate(("镜头功能", "可复用等级", "禁用说明", "来源品牌"), 12):
                ws.cell(6, col, title)
            for row in range(7, ws.max_row + 1):
                if ws.cell(row, 9).value:
                    ws.cell(row, 12, "建立空间" if row <= 10 else "互动参与" if row <= 16 else "仪式过程" if row <= 25 else "情绪收束")
                    ws.cell(row, 13, "可复用")
                    ws.cell(row, 15, "匿名年会")
        wb.save(path)
    finally:
        wb.close()
    report = refresh_ai_spec(path)
    assert report.in_sync and report.usable, report
    return path


def run_creation(root):
    """Leave a restartable anonymous project for the delivery half of the journey."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    source_root = root / "source"
    source_root.mkdir(exist_ok=True)
    workbook = write_annual_template(source_root / "M+年会类通用脚本库.xlsx")
    store = WorkspaceStore(root / "runtime" / "workspace.sqlite3")
    owner = LocalIdentityAdapter(store).bootstrap_owner(workspace_name="匿名握手验证", display_name="匿名验证用户",
        confirmed=True, token="anonymous-handshake-owner")
    token, workspace_id = owner["auth_token"], owner["workspace_id"]
    projects = ProjectService(store)
    project = projects.create_project(token, workspace_id=workspace_id, name="匿名连续补充项目",
        category_id="launch", expected_workspace_version=0)
    scope = {"workspace_id": workspace_id, "project_id": project["project_id"]}
    tasks = TaskService(store)
    briefs = FormalBriefWorkspaceService(store, artifact_root=root / "runtime")
    adapter = FormalBriefInputAdapter(briefs, data_root=root / "runtime")
    packages = AssetPackagePublicationService(registry=LocalPackageRegistry(root / "registry.json"),
        storage=LocalAssetStorage(root / "assets"), packages_root=root / "packages")
    template = packages.import_template(category_id="annual", filename=workbook.name,
        content=workbook.read_bytes(), display_name="匿名年会25镜")
    packages.registry.promote("annual", template["package_id"], template["package_version"])
    calls = []
    base_provider = formal_tests.FormalGenerationServiceTests("runTest")._provider("formal", calls)

    def provider(prompt, model, tokens):
        payload = json.loads(base_provider(prompt, model, tokens))
        source = json.loads(prompt.split("\nDATA:", 1)[1].split("\nDRAFT:", 1)[0])
        for shot, basis in zip(payload["shots"], source["shots"]):
            shot.update(basis.get("accepted_creative_fields") or {})
        return json.dumps(payload, ensure_ascii=False)

    policy = FormalTextProviderPolicy(writer=provider, editor=provider, writer_provider="anonymous",
        writer_model="anonymous", editor_provider="anonymous", editor_model="anonymous")
    service = FormalGenerationService(FormalGenerationServices(briefs=briefs, projects=projects,
        tasks=tasks, artifacts=RunArtifactResolver(root / "runs"),
        images=ProjectImagePoolService(store, data_root=root / "runtime"), knowledge=KnowledgeService(store)),
        runs_root=root / "runs", source_root=source_root, provider_policy=policy,
        brief_input_adapter=adapter, template_packages=packages,
        asset_package_resolver=lambda category: {**packages.registry.resolve_formal(category), "alias": "formal"})
    editor_inputs = []
    block_order = {}

    def revision_provider(prompt, model, tokens):
        data = json.loads(prompt.split("\nDATA:", 1)[1])
        updates = json.loads(data["context"]["recent_conversation"][0]["content"].split("：", 1)[1])
        editor_inputs.append(updates)
        facts = {item["field"]: item["value"] for item in updates}
        operations = []
        for target in data["targets"]:
            index = block_order[target["shot_id"]]
            values = {}
            if "脚本类型" in facts:
                values.update(structure=SECTIONS[index], visual=VISUALS[index])
            if "目标时长" in facts:
                values["duration_text"] = "12秒"
            if "风格调性" in facts:
                values["sound"] = "克制高级的音乐与现场环境声自然衔接"
            if target["field"] in values:
                operations.append({"shot_id": target["shot_id"], "field": target["field"], "value": values[target["field"]]})
        return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1", "operations": operations}, ensure_ascii=False)

    revision = FormalRevisionService(projects=projects, tasks=tasks, workspace=None, writes_enabled=True,
        brief_adapter=adapter, assistant_editor_policy=FormalScriptAssistantEditorPolicy(
            invoke=revision_provider, provider="anonymous", model="anonymous"))
    service.creative_revision = revision
    original = [{"段落": f"发布会段落{i}", "画面描述": f"新品发布会产品亮相画面{i}",
                 "声音": "现场氛围与音乐推进", "时长": "建议8秒"} for i in range(1, 6)]

    def stream(prompt, model, tokens, emit):
        text = json.dumps({"blocks": original}, ensure_ascii=False)
        for offset in range(0, len(text), 17):
            emit(text[offset:offset + 17])
        return text

    service.creative_stream_provider = stream
    instruction = "\n".join(("项目名称：匿名连续补充项目", "脚本类型：发布会", "品牌或客户：匿名团队",
        "视频目标：呈现团队凝聚力与活动氛围", "使用场景：团队年度活动现场", "禁止或不能出现的内容：不得虚构事实"))
    input_file = source_root / "匿名团队创作要求.txt"
    input_file.write_text(instruction, encoding="utf-8")
    started = service.start_creative_draft(token, **scope, expected_project_version=0, expected_revision=0,
        instruction=instruction, operation_key="handshake-generation", start_background=False)
    service.execute_creative_draft(token, **scope, task_id=started["task"]["task_id"])
    current = service.read_creative_draft(token, **scope)
    assert len(current["blocks"]) == 5 and current["task"]["status"] == "completed", current
    block_order.update({block["block_id"]: index for index, block in enumerate(current["blocks"])})
    previews = []
    for index, (field, value) in enumerate((("脚本类型", "年会"), ("目标时长", "60秒"), ("风格调性", "高级"))):
        revision.answer_creative_input(token, **scope, expected_project_version=current["project_version"],
            expected_revision=current["revision"], field=field, value=value, operation_key=f"handshake-answer-{index}")
        current = service.read_creative_draft(token, **scope)
        assert current["task"]["status"] == "waiting_user" and current["proposal"], current
        assert [block["fields"] for block in current["blocks"]] == original
        previews.append(current)
    assert {row["field"]: row["value"] for row in editor_inputs[-1]} == {"脚本类型": "年会", "目标时长": "60秒", "风格调性": "高级"}
    revision.confirm_creative_draft_change(token, **scope, task_id=current["task"]["task_id"],
        proposal_digest=current["proposal"]["digest"])
    accepted = service.read_creative_draft(token, **scope)
    assert not accepted["unimplemented_answers"]
    assert [block["fields"]["画面描述"] for block in accepted["blocks"]] == VISUALS
    raw = briefs.read_current_formal(token, **scope)
    assert raw.get("unresolved_conflict_count", 0) == 0, raw["brief"].get("conflicts")
    confirmed = adapter.confirm(token, **scope, brief_version_id=raw["brief_version_id"], confirmed_at=utc_now(),
        expected_project_version=accepted["project_version"])
    assert confirmed["creative_standard"]["status"] == "confirmed"
    current_project = projects.get_project(token, scope["project_id"])
    assert current_project["category_id"] == "annual", current_project
    selected = service.select_template(token, **scope, expected_project_version=current_project["project_version"],
        template_ref=template["resolved_ref"], confirmed=True)
    creative = service.read_creative_draft(token, **scope)
    plan = service.prepare_plan(token, **scope, expected_project_version=selected["project_version"],
        expected_creative_revision=creative["revision"], generation_mode="text_only_draft")
    (root / "planning-preview.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    assert plan["shot_count"] == 25, plan
    result = service.confirm_plan_and_generate(token, **scope, plan_id=plan["plan_id"],
        expected_project_version=projects.get_project(token, scope["project_id"])["project_version"])
    assert len(result["script_rows"]) == 25 and result["version_id"], result
    with store.connection() as connection:
        version = dict(connection.execute("SELECT * FROM project_versions WHERE version_id=?", (result["version_id"],)).fetchone())
        assert connection.execute("SELECT count(*) FROM version_review_human_decisions").fetchone()[0] == 0
    output_workbook = service.services.artifacts.resolve_artifact(version["run_id"], "workbook").path
    from mvp_v2.excel_io import ADJACENT_REVIEW_SHEET_NAME
    wb = load_workbook(output_workbook)
    try:
        edges = [row for row in wb[ADJACENT_REVIEW_SHEET_NAME].iter_rows(min_row=2, values_only=True) if any(value is not None for value in row)]
        assert len(edges) == 24
    finally:
        wb.close()
    summary = {"scope": scope, "version_id": result["version_id"], "run_id": version["run_id"],
        "plan_id": plan["plan_id"], "shot_count": len(result["script_rows"]), "adjacent_count": len(edges),
        "provider": "anonymous", "review_decisions": 0,
        "template": {"path": str(workbook), "sha256": sha256_file(workbook), "ref": template["resolved_ref"]},
        "input": {"path": str(input_file), "sha256": sha256_file(input_file)},
        "workbook": str(output_workbook), "database": str(store.database_path),
        "editor_inputs": editor_inputs, "previews": [item["task"]["task_id"] for item in previews],
        "source_trace_complete": all(shot["template_source"]["file"] and shot["template_source"]["sheet"] and shot["template_source"]["row"] for shot in plan["shots"])}
    (root / "creation-evidence.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"root": root, "store": store, "service": service, "token": token, "scope": scope,
        "projects": projects, "packages": packages, "plan": plan, "result": result, "summary": summary}


class HandshakeCreationTests(unittest.TestCase):
    def test_continuous_answers_reach_selected_25_shot_annual_formal_version(self):
        with tempfile.TemporaryDirectory(prefix="mplus-handshake-creation-") as directory:
            result = run_creation(Path(directory))
            self.assertEqual(result["summary"]["shot_count"], 25)
            self.assertTrue(result["summary"]["source_trace_complete"])


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--output":
        print(json.dumps(run_creation(Path(sys.argv[2]))["summary"], ensure_ascii=False, indent=2))
    else:
        unittest.main()
