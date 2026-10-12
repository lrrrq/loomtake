from __future__ import annotations

"""Isolated Formal server: deterministic fixtures or an explicit production runtime.

MPLUS_FORMAL_FIXTURE_MODE=real uses the production composition and real providers.
Use a fresh MPLUS_FORMAL_FIXTURE_ROOT; deterministic evidence is never reused.
"""

import os
import json
import hashlib
import time
from pathlib import Path
import tempfile
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import uvicorn
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

FIXTURE_MODE = os.environ.get("MPLUS_FORMAL_FIXTURE_MODE", "").strip() or (
    "real" if any(os.environ.get(key) == "1" for key in (
        "MPLUS_FORMAL_REAL_DEEPSEEK", "MPLUS_FORMAL_REAL_MINIMAX", "MPLUS_FORMAL_REAL_ADVISOR_ONLY",
    )) else "deterministic"
)
if FIXTURE_MODE not in {"deterministic", "real"}:
    raise RuntimeError("fixture 模式必须为 deterministic 或 real")
root = Path(os.environ.get("MPLUS_FORMAL_FIXTURE_ROOT") or tempfile.mkdtemp(prefix="mplus-formal-console-browser-"))
root.mkdir(parents=True, exist_ok=True)
mode_path = root / "fixture-mode.json"
if mode_path.is_file():
    if json.loads(mode_path.read_text(encoding="utf-8")).get("mode") != FIXTURE_MODE:
        raise RuntimeError("不同 fixture 模式必须使用独立目录，不能复用已有证据")
elif FIXTURE_MODE == "real" and any(root.iterdir()):
    raise RuntimeError("真实模式需要空的独立目录，不能复用未标记的 fixture 证据")
mode_path.write_text(json.dumps({"mode": FIXTURE_MODE, "production_composition": FIXTURE_MODE == "real"}) + "\n", encoding="utf-8")
if FIXTURE_MODE == "real":
    from mvp_v2.runtime_config import load_local_secrets, profile_root
    load_local_secrets(profile_root("stable") / "secrets.env")
fixture_runtime_root = root / "isolated-runtime"
fixture_runtime_root.mkdir(parents=True, exist_ok=True)
TEST_PORT = int(os.environ.get("MPLUS_FORMAL_TEST_PORT", "18770"))
os.environ.update({
    "MPLUS_CONFIG": str(root / "config.json"),
    "MPLUS_DATA_ROOT": str(root),
    "MPLUS_SECRETS": str(fixture_runtime_root / "secrets.env"),
    "MPLUS_PORT": str(TEST_PORT),
})

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.asset_package import bundle_asset_package_v2, create_asset_package, load_asset_package
from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.package_publication import AssetPackagePublicationService
from mvp_v2.package_registry import LocalPackageRegistry, build_package_record
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentServices
from mvp_v2.formal_agent import FormalExecutableAgentService, FormalExecutionBackend
from mvp_v2.formal_advisor import FormalAdvisorPolicy, FormalAdvisorService
from mvp_v2.formal_admin_service import FormalAdminService
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_default_images import FormalDefaultImageService
from mvp_v2.formal_generation_service import (
    FormalGenerationService,
    FormalGenerationServices,
    FormalTextProviderPolicy,
    FormalVisionProviderPolicy,
)
from mvp_v2.image_understanding import (
    PROJECT_IMAGE_VISION_PROMPT_VERSION,
    ProjectImageVisionResult,
    generate_minimax_project_image_analysis,
)
from mvp_v2.formal_script_workspace import (
    FormalScriptWorkspaceService,
    FormalScriptWorkspaceServices,
)
from mvp_v2.formal_image_workspace import (
    FormalImageWorkspaceService,
    FormalImageWorkspaceServices,
)
from mvp_v2.formal_review_workspace import (
    FormalReviewWorkspaceService,
    FormalReviewWorkspaceServices,
    WorkspaceBoundFinalConfirmation,
    WorkspaceBoundVersionQuality,
    WorkspaceReviewerActorResolver,
)
from mvp_v2.formal_script_revision import FormalScriptRevisionService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import create_app
from mvp_v2.project_service import ProjectService
from mvp_v2.project_image_authority import ProjectImagePoolService
from mvp_v2.manual_review_commands import ManualReviewCommandService, ManualReviewCommandServices
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.text_providers import build_text_provider
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.review_evidence_artifacts import FileReviewEvidenceArtifactStore, ReviewEvidenceAuthority
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceStore
from mvp_v2.ai_spec import refresh_ai_spec
from tests.test_mvp_v2 import (
    full_launch_template_rows,
    full_launch_visual_evidence,
    write_launch_library,
)
fixture_dir = root / "fixtures"
fixture_scenario = os.environ.get("MPLUS_FORMAL_FIXTURE_SCENARIO", "default").strip() or "default"


def _office_bytes(member: str, text: str) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(member, text)
    return output.getvalue()


def _pdf_bytes(text: str) -> bytes:
    writer = PdfWriter(); page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = DecodedStreamObject(); content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")); page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO(); writer.write(output); return output.getvalue()


def _write_fixture_files() -> None:
    fixture_dir.mkdir(parents=True, exist_ok=True)
    repository_root = Path(__file__).resolve().parents[2]
    atlas = (
        repository_root / "tests" / "fixtures" / "formal_visual_quality"
        / "anonymous-launch-atlas-a.png"
    )
    (fixture_dir / "01-brief.pdf").write_bytes(_pdf_bytes("Project: Anonymous One"))
    (fixture_dir / "02-brief.docx").write_bytes(_office_bytes("word/document.xml", "<w:document xmlns:w=\"w\"><w:body><w:p><w:r><w:t>项目名称: 匿名二号</w:t></w:r></w:p></w:body></w:document>"))
    workbook = Workbook(); sheet = workbook.active; sheet.title = "资料"
    for row, value in enumerate((
        "脚本类型: 发布会",
        "品牌或客户: 匿名品牌",
        "目标时长: 60秒",
        "风格调性: 克制、真实",
        "视频目标: 嘉宾发布",
        "必须出现的内容: 产品演示",
        "关键事件与流程: 嘉宾发布、说明发布重点、产品演示、品牌收束",
        "禁止或不能出现的内容: 不得虚构事实",
        "参考图片: 05-reference.png",
    ), start=1): sheet.cell(row, 1, value)
    workbook.save(fixture_dir / "03-brief-no-images.xlsx")
    embedded_paths = []
    for index, (cell, panel_index) in enumerate(zip(
        ("C2", "C14", "C26"), (15, 16, 20),
    ), start=1):
        embedded_path = fixture_dir / f"embedded-{index}.png"
        panel = _atlas_panel(atlas, panel_index)
        panel.putpixel((0, 0), (index * 31, index * 47, index * 59))
        panel.save(embedded_path, format="PNG")
        embedded_paths.append(embedded_path)
        embedded = ExcelImage(str(embedded_path))
        embedded.width, embedded.height = 320, 180
        sheet.add_image(embedded, cell)
    workbook.save(fixture_dir / "03-brief.xlsx")
    for embedded_path in embedded_paths:
        embedded_path.unlink(missing_ok=True)
    (fixture_dir / "04-brief.pptx").write_bytes(_office_bytes("ppt/slides/slide1.xml", "<p:sld xmlns:p=\"p\"><p:spTree><p:txBody><a:p xmlns:a=\"a\"><a:r><a:t>使用场景: 发布会现场</a:t></a:r></a:p></p:txBody></p:spTree></p:sld>"))
    for offset, number in enumerate(range(5, 23)):
        _atlas_panel(atlas, offset).save(
            fixture_dir / f"{number:02d}-reference.png", format="PNG",
        )
    if FIXTURE_MODE == "real":
        return
    metadata_dir = root / "asset-metadata"
    metadata_dir.mkdir(exist_ok=True)
    template_rows = full_launch_template_rows()
    manifest_rows = []
    analysis_rows = []
    for offset, number in enumerate(range(5, 23)):
        image_id = f"anonymous-default-{number:02d}"
        image_path = fixture_dir / f"{number:02d}-reference.png"
        template = template_rows[offset]
        caption, tags = full_launch_visual_evidence(offset + 1)
        caption = (
            caption.replace("Midea", "匿名品牌")
            .replace("美的", "匿名品牌")
            .replace("智美万象", "匿名主题")
        )
        tags = [
            str(tag).replace("Midea", "匿名品牌").replace("智美万象", "匿名主题")
            for tag in tags
        ]
        manifest_rows.append({"image_id": image_id, "原图路径": str(image_path)})
        analysis_rows.append({
            "image_id": image_id,
            "caption": caption,
            "tags": [*tags, str(template[0] or "发布会"), str(template[4] or "现场")],
            "subjects": ["匿名发布会现场"],
            "scene": "匿名发布会现场",
            "purpose": str(template[8] or "发布会镜头参考"),
            "quality": "匿名 fixture 已人工核验，画面清晰可用",
            "brand_names": ["无"],
            "negative_flags": [],
            "ocr_text": "",
            "vision_recognition_status": "succeeded",
            "review_status": "reviewed",
            "source": "anonymous_fixture_human_review",
        })
    (metadata_dir / "image_manifest.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in manifest_rows),
        encoding="utf-8",
    )
    (metadata_dir / "image_analysis.minimax.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in analysis_rows),
        encoding="utf-8",
    )


def _write_qingyu_fixture_files() -> None:
    """Create the bounded, representative Star Activity browser fixture."""
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / "01-brief.pdf").write_bytes(_pdf_bytes("Qingyu Star Activity Brief"))
    (fixture_dir / "02-brief.docx").write_bytes(_office_bytes(
        "word/document.xml",
        "<w:document xmlns:w=\"w\"><w:body><w:p><w:r><w:t>Qingyu Star Activity</w:t></w:r></w:p></w:body></w:document>",
    ))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Brief"
    for row, value in enumerate((
        "项目名称: 青屿明星见面日",
        "品牌或客户: 青屿品牌",
        "脚本类型: 明星活动",
        "行业或活动类别: 明星活动",
        "视频目标: 记录明星见面日的互动与产品体验，带动现场参与",
        "使用场景: 青屿城市艺术中心见面日主会场",
        "目标时长: 60秒",
        "风格调性: 轻快、有活力、亲近自然",
        "必须出现的内容: 明星互动、产品展示",
        "关键事件与流程: 明星互动、产品展示",
        "交付要求: 输出 8 镜脚本 Excel 与 PDF，逐镜保留图片来源证据",
    ), start=1):
        sheet.cell(row, 1, value)
    workbook.save(fixture_dir / "03-brief.xlsx")
    workbook.save(fixture_dir / "03-brief-no-images.xlsx")
    workbook.close()
    (fixture_dir / "04-brief.pptx").write_bytes(_office_bytes(
        "ppt/slides/slide1.xml",
        "<p:sld xmlns:p=\"p\"><p:spTree><a:p xmlns:a=\"a\"><a:r><a:t>Qingyu Star Activity</a:t></a:r></a:p></p:spTree></p:sld>",
    ))

    colors = ((38, 92, 160), (68, 148, 120), (232, 156, 62), (164, 91, 150), (191, 74, 82), (76, 76, 76))
    for index, color in enumerate(colors, start=1):
        Image.new("RGB", (1280, 720), color=color).save(
            fixture_dir / f"qingyu-project-{index:02d}.png", format="PNG",
        )
    Image.new("RGB", (1280, 720), color=(40, 130, 165)).save(
        fixture_dir / "qingyu-creative-distinct.png", format="PNG",
    )
    close = Image.new("RGB", (1280, 720), color=(197, 116, 91))
    close.save(fixture_dir / "qingyu-creative-close-a.png", format="PNG")
    close.putpixel((640, 360), (198, 117, 92))
    close.save(fixture_dir / "qingyu-creative-close-b.png", format="PNG")


def _qingyu_template_rows() -> list[list[object]]:
    structures = (
        "入场欢迎", "入场欢迎", "明星互动", "明星互动",
        "产品展示", "产品展示", "粉丝参与", "品牌收束",
    )
    descriptions = (
        "活动入口与粉丝入场建立空间",
        "明星从入口进入并向粉丝挥手",
        "明星与粉丝近距离交流互动",
        "明星带领粉丝完成现场互动动作",
        "明星在产品体验区展示产品亮点",
        "粉丝参与产品体验并记录现场瞬间",
        "明星与粉丝完成合影和挥手告别",
        "活动主视觉与品牌信息收束落版",
    )
    return [
        [
            structures[index - 1], index, None, None, "轻快切换", "手持", "中景",
            "跟随推进", descriptions[index - 1], "现场欢呼与轻快音乐", "",
            "可复用", "", "青屿品牌", "青屿活动 fixture 已人工核验",
        ]
        for index in range(1, 9)
    ]


def _qingyu_creative_rows() -> list[list[object]]:
    return [
        ["1", "入场欢迎", "现场纪实", None, "贴纸动效", "手持", "全景", "横移", "粉丝沿活动入口快速入场", "轻快音乐", ""],
        ["3", "明星互动", "近景捕捉", None, "闪白转场", "手持", "近景", "推近", "明星与粉丝交换手写卡片", "现场欢呼", ""],
        ["5", "产品展示", "体验记录", None, "节奏剪辑", "手持", "中近景", "环绕", "明星与粉丝共同体验产品", "轻快音乐", ""],
        ["8", "品牌收束", "主视觉落版", None, "干净定格", "固定", "全景", "固定", "活动主视觉与品牌信息收束落版", "轻快音乐收束", ""],
    ]


def _write_qingyu_source_workbook(path: Path) -> None:
    write_launch_library(path, _qingyu_template_rows(), _qingyu_creative_rows())
    workbook = load_workbook(path)
    try:
        for sheet_name, start_row, row_count in (("模版", 7, 8), ("创意库", 7, 4)):
            sheet = workbook[sheet_name]
            headers = {
                12: "可复用等级", 13: "禁用说明", 14: "来源品牌", 15: "品牌中性依据",
            }
            for column, value in headers.items():
                sheet.cell(6, column, value)
            for row in range(start_row, start_row + row_count):
                sheet.cell(row, 12, "可复用")
                sheet.cell(row, 13, "")
                sheet.cell(row, 14, "青屿品牌")
                sheet.cell(row, 15, "青屿活动 fixture 已人工核验")
        for row, name in zip((7, 8, 9, 10), (
            "qingyu-creative-distinct.png",
            "qingyu-creative-close-a.png",
            "qingyu-creative-close-b.png",
            "qingyu-creative-distinct.png",
        )):
            image = ExcelImage(str(fixture_dir / name))
            image.width, image.height = 320, 180
            workbook["创意库"].add_image(image, f"D{row}")
        workbook.save(path)
    finally:
        workbook.close()


def _atlas_panel(path: Path, index: int) -> Image.Image:
    with Image.open(path) as source:
        image = source.convert("RGB")
        width, height = image.size
        column, row = index % 4, index // 4
        left, right = round(column * width / 4), round((column + 1) * width / 4)
        top, bottom = round(row * height / 6), round((row + 1) * height / 6)
        inset = 8
        return image.crop((left + inset, top + inset, right - inset, bottom - inset)).resize((1280, 720))


def _uploaded_document(filename: str, stream, critical: bool) -> UploadedDocument:
    return UploadedDocument(filename=filename, stream=stream, critical=critical)


if fixture_scenario == "qingyu_star_activity":
    if not all((fixture_dir / name).is_file() for name in (
        "01-brief.pdf", "02-brief.docx", "03-brief.xlsx",
        "03-brief-no-images.xlsx", "04-brief.pptx",
        *(f"qingyu-project-{index:02d}.png" for index in range(1, 7)),
        "qingyu-creative-distinct.png", "qingyu-creative-close-a.png",
        "qingyu-creative-close-b.png",
    )):
        _write_qingyu_fixture_files()
else:
    if not all((fixture_dir / name).is_file() for name in (
        "01-brief.pdf", "02-brief.docx", "03-brief.xlsx",
        "03-brief-no-images.xlsx", "04-brief.pptx",
        *(f"{number:02d}-reference.png" for number in range(5, 23)),
    )):
        _write_fixture_files()
source_root = root / "source"
source_root.mkdir(exist_ok=True)
source_workbook = source_root / (
    "M+明星活动类通用脚本库.xlsx"
    if fixture_scenario == "qingyu_star_activity"
    else "M+发布会类通用脚本库.xlsx"
)
if not source_workbook.is_file():
    if fixture_scenario == "qingyu_star_activity":
        _write_qingyu_source_workbook(source_workbook)
    else:
        write_launch_library(source_workbook, full_launch_template_rows(), [])
        source = load_workbook(source_workbook)
        for row in range(7, 25):
            reference = ExcelImage(str(fixture_dir / f"{row - 2:02d}-reference.png"))
            reference.width, reference.height = 320, 180
            source["模版"].add_image(reference, f"C{row}")
            source["模版"].cell(row, 10, "现场氛围与音乐推进")
        source.save(source_workbook)
        source.close()
    refresh_ai_spec(source_workbook)
package_v1 = root / (
    "asset-package-qingyu-v1" if fixture_scenario == "qingyu_star_activity"
    else "asset-package-v1"
)
package_v2 = root / (
    "asset-package-qingyu-v2" if fixture_scenario == "qingyu_star_activity"
    else "asset-package-v2"
)
if not (package_v2 / "asset_package.json").is_file():
    package_manifest = create_asset_package(
        package_v1,
        package_id=(
            "formal-browser-qingyu-star-activity"
            if fixture_scenario == "qingyu_star_activity"
            else "formal-browser-default-images"
        ),
        source_root=source_root,
        media_roots=[fixture_dir],
        metadata_roots=[root / "asset-metadata"],
        category_id=("star_activity" if fixture_scenario == "qingyu_star_activity" else "launch"),
    )
    bundle_asset_package_v2(package_manifest, package_v2, package_version=1)
package_manifest = package_v2 / "asset_package.json"


def _formal_source_root(_script_type: str) -> Path:
    return Path(load_asset_package(package_manifest)["source_root_path"]).resolve()




def _provider_business_input(prompt: str, model: str) -> tuple[list[dict], list[dict]]:
    data_text = prompt.split("\nDATA:", 1)[1].split("\nDRAFT:", 1)[0]
    data = json.loads(data_text)
    shots = list(data.get("shots") or [])
    images = list((data.get("project_image_context") or {}).get("images") or [])
    if not shots:
        raise RuntimeError("测试 Provider 只能消费 P00 传入的动态镜头清单")
    image_caption = (
        str(images[0].get("caption") or "").strip()
        if images else "文字草稿无图片"
    )
    provider_log = root / "provider-calls.jsonl"
    with provider_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"model": model, "shot_count": len(shots), "image_caption": image_caption}, ensure_ascii=False) + "\n")
    return shots, images


def _deterministic_provider(prompt: str, _model: str, _max_tokens: int) -> str:
    shots, _images = _provider_business_input(prompt, _model)
    # Project images arrive in UUID order. A pool caption is not a per-shot
    # decision; keep this fixture on each shot's canonical planning basis.
    return json.dumps({
        "schema_version": "mplus-script-draft-response/v1",
        "overall_summary": "依据已确认 Brief、创作标准卡和项目图片理解生成可拍脚本。",
        "shots": [
            {
                "shot_no": str(shot.get("shot_no") or ""),
                "visual_description": f"第{shot.get('shot_no')}镜：{str(shot.get('basis') or shot['planning_contract']['primary_event']).rstrip('。')}。",
                "sound": "现场音乐推进",
                "voiceover": "",
                "subtitle": "",
                "pending_confirmations": [],
            }
            for shot in shots
        ],
    }, ensure_ascii=False)


_fixture_provider = _deterministic_provider
_fixture_provider_name = "anonymous-fixture-writer"
_fixture_model = "anonymous-fixture-writer-model"


def _advisor_provider(prompt: str, model: str, max_tokens: int) -> str:
    user = prompt.rsplit("\nUSER:", 1)[-1]
    image_request = "配图" in user and ("备选" in user or "方案" in user)
    return json.dumps({
        "intent": "inspect_image_candidates" if image_request else "confirm_generation",
        "summary": "读取权威图片候选。" if image_request else "检查当前生成条件。",
        "evidence_ids": [], "questions": [],
        "candidate_action": "none" if image_request else "confirm_formal_plan_generation",
        "image_query": "",
    }, ensure_ascii=False)


_qingyu_provider_status_by_sha: dict[str, str] = {}
if fixture_scenario == "qingyu_star_activity":
    _qingyu_provider_status_by_sha = {
        hashlib.sha256((fixture_dir / f"qingyu-project-{index:02d}.png").read_bytes()).hexdigest(): status
        for index, status in (
            (1, "provider_failure"), (2, "pass"), (3, "low_confidence"),
            (4, "provider_failure"), (5, "pass"), (6, "blocked"),
        )
    }


def _qingyu_vision(path: Path) -> ProjectImageVisionResult:
    status = _qingyu_provider_status_by_sha.get(
        hashlib.sha256(path.read_bytes()).hexdigest(), "pass",
    )
    if status == "provider_failure":
        raise RuntimeError("deterministic visual provider fixture failure")
    return ProjectImageVisionResult(
        status=status,
        caption="青屿明星见面日现场图片，主体与活动场景清晰可辨",
        tags=("明星活动", "粉丝互动", "青屿"),
        subjects=("明星", "活动参与者"),
        scene="青屿城市艺术中心见面日现场",
        purpose="支持明星互动与产品展示镜头",
        quality="像素清晰，主体可辨认",
        brand="青屿品牌",
        evidence=("qingyu_star_activity deterministic fixture",),
    )


fixture_template_registry = LocalPackageRegistry(root / "fixture-template-registry.json")
fixture_template_registry.register_records([build_package_record(package_manifest)], promote_formal=True)
fixture_templates = AssetPackagePublicationService(
    storage=LocalAssetStorage(root / "fixture-template-storage"),
    registry=fixture_template_registry, packages_root=root / "fixture-template-packages",
)

if FIXTURE_MODE == "real":
    from mvp_v2.formal_runtime import build_formal_runtime
    formal_runtime_bundle = build_formal_runtime({
        "data_root": str(root / "data"), "runs_root": str(root / "runs"),
        "source_root": str(source_root), "asset_package": str(package_manifest),
        "asset_packages": {"star_activity" if fixture_scenario == "qingyu_star_activity" else "launch": str(package_manifest)},
        "package_registry_path": str(root / "fixture-template-registry.json"),
        "packages_root": str(root / "fixture-template-packages"),
        "formal_agent_read_only": False,
    })
    agent = formal_runtime_bundle.agent
    adapter = formal_runtime_bundle.brief_adapter
    generation = formal_runtime_bundle.generation
    script_workspace = formal_runtime_bundle.script_workspace
    image_workspace = formal_runtime_bundle.image_workspace
    review_workspace = formal_runtime_bundle.review_workspace
    store = agent.services.store
    identity = agent.services.identity
    projects = agent.services.projects
    run_artifacts = agent.services.artifacts
else:
    store = WorkspaceStore(root / "workspace.sqlite3")
    identity = LocalIdentityAdapter(store)
    projects = ProjectService(store)
    run_artifacts = RunArtifactResolver(root / "runs")
    readonly_agent = ReadonlyAgentService(ReadonlyAgentServices(
        store=store, identity=identity, projects=projects, tasks=TaskService(store),
        versions=VersionService(store), artifacts=run_artifacts,
    ))
    adapter = FormalBriefInputAdapter(FormalBriefWorkspaceService(store, artifact_root=root / "data"), data_root=root / "data")


    generation = FormalGenerationService(
        FormalGenerationServices(
            briefs=adapter.service,
            projects=projects,
            tasks=TaskService(store),
            artifacts=run_artifacts,
            images=ProjectImagePoolService(store, data_root=root / "data"),
        ),
        runs_root=root / "runs",
        source_root=source_root,
        asset_package_resolver=lambda _script_type: package_manifest,
        template_packages=fixture_templates,
        provider_policy=FormalTextProviderPolicy(
            writer=_fixture_provider,
            editor=_fixture_provider,
            writer_provider=_fixture_provider_name,
            writer_model=_fixture_model,
            editor_provider=_fixture_provider_name,
            editor_model=_fixture_model,
        ),
        vision_provider_policy=(
            FormalVisionProviderPolicy(
                invoke=_qingyu_vision,
                provider="deterministic-visual-test-provider",
                model="qingyu-star-activity-matrix-v1",
                prompt_version=PROJECT_IMAGE_VISION_PROMPT_VERSION,
                max_batch_size=6,
            ) if fixture_scenario == "qingyu_star_activity" else None
        ),
    )


    delivery = VersionDeliveryService(store, run_artifacts)
    quality = WorkspaceBoundVersionQuality(
        store=store,
        delivery=delivery,
        artifacts=run_artifacts,
        data_root=root / "data",
    )
    script_commands = ManualReviewCommandService(ManualReviewCommandServices(
        store=store,
        delivery=delivery,
        quality=quality,
        final_confirmation=WorkspaceBoundFinalConfirmation(
            quality=quality,
            actor_resolver=WorkspaceReviewerActorResolver(store),
        ),
        evidence_authority=ReviewEvidenceAuthority(
            FileReviewEvidenceArtifactStore(root / "review-evidence")
        ),
    ))
    image_workspace = FormalImageWorkspaceService(
        FormalImageWorkspaceServices(
            store=store,
            projects=projects,
            delivery=delivery,
            artifacts=run_artifacts,
            images=ProjectImagePoolService(store, data_root=root / "data"),
        ),
        runs_root=root / "runs",
        source_root=source_root,
        source_root_resolver=_formal_source_root,
    )
    script_workspace = FormalScriptWorkspaceService(
        FormalScriptWorkspaceServices(
            store=store,
            projects=projects,
            delivery=delivery,
            commands=script_commands,
            artifacts=run_artifacts,
            image_workspace=image_workspace,
        ),
        runs_root=root / "runs",
        source_root=source_root,
        source_root_resolver=_formal_source_root,
    )
    _delay_script_table_ms = int(os.environ.get("MPLUS_FORMAL_DELAY_SCRIPT_TABLE_MS", "0") or "0")
    if _delay_script_table_ms > 0:
        _original_apply_table_change = script_workspace.apply_table_change
        _delayed_table_change_calls = 0

        def _delayed_apply_table_change(*args, **kwargs):
            global _delayed_table_change_calls
            if _delayed_table_change_calls == 0:
                _delayed_table_change_calls += 1
                time.sleep(_delay_script_table_ms / 1000)
            return _original_apply_table_change(*args, **kwargs)

        script_workspace.apply_table_change = _delayed_apply_table_change
    review_workspace = FormalReviewWorkspaceService(
        FormalReviewWorkspaceServices(
            store=store,
            projects=projects,
            delivery=delivery,
            artifacts=run_artifacts,
            commands=script_commands,
            image_workspace=image_workspace,
        )
    )
    script_revision = FormalScriptRevisionService(
        projects=projects,
        tasks=TaskService(store),
        workspace=script_workspace,
        writes_enabled=True,
        image_workspace=image_workspace,
        brief_adapter=adapter,
    )
    default_images = FormalDefaultImageService(
        projects=projects,
        tasks=TaskService(store),
        delivery=delivery,
        artifacts=run_artifacts,
        image_pool=image_workspace.services.images,
        image_workspace=image_workspace,
        asset_package_resolver=lambda _script_type: package_manifest,
        writes_enabled=True,
    )
    agent = FormalExecutableAgentService(
        ExecutableAgentServices(
            readonly=readonly_agent,
            projects=projects,
            tasks=TaskService(store),
            confirmations=ActionConfirmationService(
                store, b"formal-browser-fixture-secret-key",
            ),
            idempotency=ToolIdempotencyService(store),
            brief_store=BriefArtifactStore(root / "data"),
            backend=FormalExecutionBackend(),
        ),
        generation=generation,
        advisor=FormalAdvisorService(FormalAdvisorPolicy(
            invoke=_advisor_provider,
            provider="anonymous-fixture-advisor",
            model="anonymous-fixture-advisor-model",
        )),
        writes_enabled=True,
        script_revision=script_revision,
        default_images=default_images,
        review_workspace=review_workspace,
    )
app = create_app(
    agent_api=agent,
    formal_brief_adapter=adapter,
    formal_brief_upload_factory=_uploaded_document,
    formal_generation_service=generation,
    formal_script_service=script_workspace,
    formal_image_service=image_workspace,
    formal_review_service=review_workspace,
    formal_admin_service=formal_runtime_bundle.admin if FIXTURE_MODE == "real" else FormalAdminService(store),
)

if __name__ == "__main__":
    print(f"MPLUS_FORMAL_FIXTURE_MODE={FIXTURE_MODE}", flush=True)
    print(f"MPLUS_FORMAL_FIXTURE_DIR={fixture_dir}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=TEST_PORT, log_level="warning")
