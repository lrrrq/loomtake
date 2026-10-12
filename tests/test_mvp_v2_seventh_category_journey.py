"""Anonymous category configuration must reach the real Formal generation path.

The worker is a fresh process: production modules consume the startup config
before import.  It never patches category decisions, packages, or DB rows.
"""

from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CATEGORY_ID = "anonymous_roadshow"
CATEGORY_LABEL = "匿名路演"


def anonymous_category_config(root: Path) -> Path:
    source = REPOSITORY_ROOT / "mvp_v2" / "category_definitions.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["items"].append({
        "category_id": CATEGORY_ID, "display_name": CATEGORY_LABEL,
        "source_workbook_name": "匿名路演脚本库.xlsx",
        "source_sheet_names": ["模版", "创意库", "AI_规范表"],
        "dynamic_shot_count": True, "workbook_glob": "匿名路演脚本库*.xlsx",
        "aliases": ["anonymous-roadshow"],
        "planning_profile": "ai_spec_function_contract",
        "audit_profile": "data_driven_category_contract",
        "duration_policy": "positive_integer_seconds",
        "cross_category_sources": False,
        "structural_arcs": [
            [1, 3, "现场建立", ["space", "arrival"]],
            [4, 7, "展品展示", ["product", "detail"]],
            [8, 10, "观众体验", ["interaction", "crowd"]],
            [11, 12, "结束收束", ["closing"]],
        ],
        "pacing_profile": "even",
    })
    path = root / "anonymous-categories.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_seventh_category_journey(root: Path) -> dict:
    """Real services and workbooks; only the writer/editor are anonymous fixtures."""
    # Imports stay here so the worker's startup environment is authoritative.
    from io import BytesIO

    from openpyxl import load_workbook
    from mvp_v2.ai_spec import refresh_ai_spec
    from mvp_v2.asset_package import bundle_asset_package_v3, package_script_catalog
    from mvp_v2.asset_storage import LocalAssetStorage
    from mvp_v2.formal_brief_input_adapter import UploadedDocument
    from mvp_v2.formal_generation_service import FormalTextProviderPolicy
    from mvp_v2.generation_stages import read_visual_binding
    from mvp_v2.package_publication import AssetPackagePublicationService
    from mvp_v2.package_registry import LocalPackageRegistry, build_package_record
    from mvp_v2.script_type_adapter import SCRIPT_TYPE_METADATA, metadata_for_category_id
    from tests.test_mvp_v2_brief_category_creation import _fixture
    from tests.test_mvp_v2_formal_generation_service import FormalGenerationServiceTests
    from tests.test_mvp_v2 import write_launch_library

    evidence = {"category_id": CATEGORY_ID, "external_provider_calls": 0, "completed_stages": []}
    stage = "startup_configuration"
    try:
        assert len(SCRIPT_TYPE_METADATA) == 7, "实际服务未加载匿名第七类启动配置"
        assert metadata_for_category_id(CATEGORY_ID).display_name == CATEGORY_LABEL
        evidence["completed_stages"].append(stage)

        stage = "brief_identification_and_confirmation"
        store, owner, projects, adapter = _fixture(root / "runtime")
        token, workspace_id = owner["auth_token"], owner["workspace_id"]
        project = projects.create_project(token, workspace_id=workspace_id,
            name="匿名路演整链验收", category_id=None, expected_workspace_version=0)
        assert project["category_id"] is None
        brief_text = "\n".join((
            "项目名称：匿名路演整链验收", f"脚本类型：{CATEGORY_LABEL}",
            "品牌或客户：匿名品牌", "目标时长：60秒", "风格调性：克制真实",
            "视频目标：记录现场建立、展品展示与观众体验",
            "使用场景：线下路演现场", "必须出现的内容：展品展示",
            "关键事件与流程：现场建立、展品展示、观众体验、结束收束",
            "禁止或不能出现的内容：不得虚构未确认信息",
        ))
        pending = adapter.import_uploads(token, workspace_id=workspace_id,
            project_id=project["project_id"], expected_project_version=0,
            uploads=[UploadedDocument("匿名路演需求.txt", BytesIO(brief_text.encode("utf-8")))])
        assert pending["routing"]["status"] == "ready", pending["routing"]
        assert pending["routing"]["category_id"] == CATEGORY_ID
        assert projects.get_project(token, project["project_id"])["category_id"] is None
        confirmed = adapter.confirm(token, workspace_id=workspace_id,
            project_id=project["project_id"], brief_version_id=pending["brief_key"],
            confirmed_at="2026-09-08T00:00:00Z", expected_project_version=1)
        assert confirmed["confirmation_status"] == "confirmed"
        project = projects.get_project(token, project["project_id"])
        assert project["category_id"] == CATEGORY_ID
        evidence["completed_stages"].append(stage)

        stage = "template_import_and_registration"
        source_root = root / "template-inputs"
        segments = (
            (("现场建立", 3), ("展品展示", 4), ("观众体验", 3), ("结束收束", 2)),
            (("现场建立", 2), ("展品展示", 5), ("观众体验", 2), ("结束收束", 3)),
        )
        visuals = (
            "广角呈现路演会场外立面与入口", "来宾在入口领取活动手册",
            "镜头沿展位通道展示观众入场", "讲解员向观众介绍展品外观",
            "手部特写呈现展品按钮与纹理", "工作人员演示展品操作流程",
            "侧面中景观察展品操作后的反馈", "观众在体验台前轮流操作展品",
            "两位观众面对面交流体验感受", "主持人示意观众移步展示区域",
            "镜头拉远呈现来宾离开展位", "全景定格路演现场的完整空间",
        )
        paths = []
        for index, structure in enumerate(segments):
            folder = source_root / str(index)
            folder.mkdir(parents=True)
            path = folder / f"匿名方案{'甲' if index == 0 else '乙'}.xlsx"
            structures = [name for name, count in structure for _ in range(count)]
            rows = [[name, shot, None, None, "光效", "地面", "全景", "固定",
                     visuals[shot - 1], "现场声音", ""]
                    for shot, name in enumerate(structures, start=1)]
            creative = [[shot, "展品展示", "固定镜头", None, "光效", "地面", "中景", "固定",
                         visuals[shot + 2], "现场声音", ""] for shot in range(1, 4)]
            write_launch_library(path, rows, creative)
            book = load_workbook(path)
            try:
                book["模版"]["A3"] = f"匿名路演方案{'甲' if index == 0 else '乙'}"
                book["模版"].column_dimensions["I"].width = 73 + index * 8
                for sheet_name in ("模版", "创意库"):
                    sheet = book[sheet_name]
                    for column, title in enumerate(("镜头功能", "可复用等级", "禁用说明",
                                                    "来源品牌", "画面主体", "标签"), start=12):
                        sheet.cell(6, column, title)
                    for row in range(7, sheet.max_row + 1):
                        for column, value in enumerate(("路演现场记录", "可复用", "",
                                                       "匿名品牌", "观众与展品", "路演; 展品; 观众"), start=12):
                            sheet.cell(row, column, value)
                book.save(path)
            finally:
                book.close()
            report = refresh_ai_spec(path)
            assert report.usable and report.in_sync, report.validation_codes
            paths.append(path)
        registry = LocalPackageRegistry(root / "registry.json")
        packages = AssetPackagePublicationService(registry=registry,
            storage=LocalAssetStorage(root / "assets"), packages_root=root / "packages")
        records = [packages.import_template(category_id=CATEGORY_ID, filename=path.name,
            content=path.read_bytes(), display_name=f"匿名方案{index + 1}") for index, path in enumerate(paths)]
        assert len({record["resolved_ref"] for record in records}) == 2
        assert CATEGORY_ID not in registry.list()["aliases"]
        assert packages.import_template(category_id=CATEGORY_ID, filename=paths[1].name,
            content=paths[1].read_bytes(), display_name="匿名方案2")["resolved_ref"] == records[1]["resolved_ref"]
        for path, record in zip(paths, records):
            shots, _, _ = package_script_catalog(record["local_path"], expected_category_id=CATEGORY_ID)
            assert len(shots) == 12
            assert [(shot.source_file, shot.source_sheet, shot.source_row) for shot in shots] == [
                (path.name, "模版", row) for row in range(7, 19)]
        registry.promote(CATEGORY_ID, records[0]["package_id"], records[0]["package_version"])
        evidence["template_refs"] = [item["resolved_ref"] for item in records]
        evidence["completed_stages"].append(stage)

        stage = "trial_selection_and_planning"
        fixture = FormalGenerationServiceTests("runTest")
        fixture.root, fixture.source_root = root, source_root
        fixture.store, fixture.projects, fixture.briefs = store, projects, adapter.service
        fixture.knowledge = None
        calls = []
        policy = FormalTextProviderPolicy(writer=fixture._provider("writer", calls),
            editor=fixture._provider("editor", calls), writer_provider="anonymous-writer",
            writer_model="anonymous-writer-model", editor_provider="anonymous-editor",
            editor_model="anonymous-editor-model")
        resolver = lambda _: {**registry.resolve_formal(CATEGORY_ID), "alias": "formal"}
        service = fixture._service(policy, asset_package_resolver=resolver)
        service.template_packages = packages
        scope = {"workspace_id": workspace_id, "project_id": project["project_id"]}
        assert len(service.template_catalog(token, **scope)["items"]) == 2
        trial = service.trial_template(token, **scope, expected_project_version=2,
            template_ref=records[1]["resolved_ref"])
        assert trial["plan"]["status"] == "trial_completed"
        assert calls == []
        assert service.list_waiting_plans(token, **scope)["items"] == []
        with store.connection() as connection:
            assert connection.execute("SELECT count(*) FROM project_versions").fetchone()[0] == 0
        selected = service.select_template(token, **scope, expected_project_version=2,
            template_ref=records[1]["resolved_ref"], confirmed=True)
        assert selected["selected_ref"] == records[1]["resolved_ref"]
        preview = service.prepare_plan(token, **scope, expected_project_version=3,
            generation_mode="text_only_draft")
        assert preview["shot_count"] >= 8
        assert preview["template_summary"]["selection_mode"] == "explicit"
        evidence["completed_stages"].append(stage)

        stage = "generation_rendering_and_frozen_history"
        old_files = {item["path"]: item["sha256"] for item in records[1]["files"]}
        upgraded = bundle_asset_package_v3(records[0]["local_path"], root / "upgraded", package_version=2)
        upgrade_record = build_package_record(upgraded)
        registry.register_records([upgrade_record], promote_formal=True)
        restarted = fixture._service(policy, asset_package_resolver=resolver)
        restarted.template_packages = packages
        assert restarted.get_plan_preview(token, **scope, plan_id=preview["plan_id"]) == preview
        result = restarted.confirm_plan_and_generate(token, **scope,
            plan_id=preview["plan_id"], expected_project_version=4)
        assert result["status"] == "completed", result
        assert len(result["script_rows"]) == preview["shot_count"]
        assert len(calls) == 2
        frozen = restarted._plan_event(token, preview["plan_id"])["formal_asset_package"]
        assert frozen["resolved_ref"] == records[1]["resolved_ref"]
        completed = restarted.services.tasks.list_task_events(token, preview["plan_id"])[-1]["details"]
        workbook = restarted.services.artifacts.resolve_artifact(completed["run_id"], "workbook")
        book = load_workbook(workbook.path)
        try:
            assert book["脚本"]["A3"].value == "匿名路演方案乙"
            assert book["脚本"].column_dimensions["I"].width == 81
            assert book["相邻镜头复核"].max_row - 1 == preview["shot_count"] - 1
        finally:
            book.close()
        binding = read_visual_binding(restarted.services.artifacts.resolve_stage_artifact(
            completed["run_id"], "visual_binding").path)
        assert len(binding.script_rows) == preview["shot_count"]
        assert all(row.get("采用来源文件") and row.get("采用来源 sheet") and row.get("采用来源行")
                   for row in binding.detail_rows)
        selected_package = Path(records[1]["local_path"])
        assert all(_digest(selected_package / name) == digest for name, digest in old_files.items())
        assert registry.resolve_formal(CATEGORY_ID)["resolved_ref"] == upgrade_record["package_id"] + "@2"
        evidence.update(shot_count=preview["shot_count"], adjacent_review_count=preview["shot_count"] - 1,
                        rendered_workbook=str(workbook.path), rendered_sha256=_digest(workbook.path),
                        anonymous_text_calls=len(calls), selected_ref=frozen["resolved_ref"])
        evidence["completed_stages"].append(stage)
        evidence["status"] = "passed"
        return evidence
    except Exception as exc:
        evidence.update(status="failed", failed_stage=stage, error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        (root / "seventh-category-result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class SeventhCategoryJourneyTests(unittest.TestCase):
    def test_isolated_seventh_category_reaches_brief_template_generation_and_render(self):
        evidence_root = os.environ.get("MPLUS_V3_EVIDENCE_ROOT", "").strip()
        if evidence_root:
            Path(evidence_root).mkdir(parents=True, exist_ok=True)
        directory = (nullcontext(tempfile.mkdtemp(prefix="mplus-seventh-category-", dir=evidence_root))
                     if evidence_root else tempfile.TemporaryDirectory(prefix="mplus-seventh-category-"))
        with directory as temporary:
            root = Path(temporary)
            config = anonymous_category_config(root)
            original = REPOSITORY_ROOT / "mvp_v2" / "category_definitions.json"
            original_sha = _digest(original)
            environment = os.environ.copy()
            environment.update(MPLUS_CATEGORY_DEFINITIONS=str(config), PYTHONPATH=str(REPOSITORY_ROOT))
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", str(root)],
                cwd=REPOSITORY_ROOT, env=environment, text=True, capture_output=True, timeout=180)
            (root / "worker.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            self.assertEqual(_digest(original), original_sha)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            evidence = json.loads((root / "seventh-category-result.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["status"], "passed")
            self.assertEqual(evidence["external_provider_calls"], 0)
            self.assertEqual(len(evidence["completed_stages"]), 5)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--worker":
    try:
        print(json.dumps(run_seventh_category_journey(Path(sys.argv[2])), ensure_ascii=False))
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
