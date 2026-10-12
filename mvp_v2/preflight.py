from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Literal

from openpyxl import load_workbook

from .auto_pipeline import project_cache_dir, resolve_project_media_root
from .excel_io import (
    IMAGE_EXTENSIONS,
    REQUIRED_INPUT_COLUMNS,
    BriefError,
    load_creative_variants,
    load_template_slots,
    read_briefs,
    read_text_brief_file,
)
from .image_understanding import minimax_credentials_available
from .asset_package import (
    AssetPackageError,
    asset_package_index_summary,
    asset_package_source_record,
    load_asset_package,
)
from .text_providers import provider_credentials_available
from .models import validate_script_brief
from .launch_contract import template_count_recommended, template_count_valid
from .script_type_adapter import (
    category_id_for_script_type,
    metadata_for_script_type,
    normalize_script_type,
    resolve_script_type_workbook,
    route_contract_for_script_type,
)
from .video_frames import VIDEO_EXTENSIONS


CheckStatus = Literal["pass", "warn", "fail"]

VIDEO_FRAME_ESTIMATE = 12
WARN_VISUAL_ANALYSIS_COUNT = 120
MAX_VISUAL_ANALYSIS_COUNT = 180


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    message: str


@dataclass(frozen=True)
class PreflightReport:
    brief: dict | None
    project_dir: Path | None
    output_dir: Path
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return not any(check.status == "fail" for check in self.checks)

    def render(self) -> str:
        lines = ["脚本运行前检查"]
        for check in self.checks:
            label = {"pass": "通过", "warn": "警告", "fail": "失败"}[check.status]
            lines.append(f"[{label}] {check.name}：{check.message}")
        lines.append("检查结论：" + ("可以开始运行" if self.passed else "请先修复失败项"))
        return "\n".join(lines)


def run_launch_preflight(
    *,
    brief_path: str | Path | None = None,
    brief_file: str | Path | None = None,
    brief_data: dict | None = None,
    source_root: str | Path,
    project_dir: str | Path | None = None,
    output_dir: str | Path,
    asset_cache_root: str | Path,
    minimax_command: str = "mmx",
    api_key: str | None = None,
    allow_cached_analysis: bool = False,
    quality_profile: str = "draft",
    writer_provider: str = "minimax-cli",
    editor_provider: str = "minimax-cli",
    asset_package: str | Path | None = None,
    prepare_output: bool = True,
) -> PreflightReport:
    checks: list[CheckResult] = []
    source_root = Path(source_root).expanduser()
    loaded_asset_package = None
    if asset_package:
        try:
            loaded_asset_package = load_asset_package(asset_package)
        except (AssetPackageError, OSError, json.JSONDecodeError) as exc:
            checks.append(CheckResult("资产包", "fail", str(exc)))
    output_dir = Path(output_dir).expanduser()
    asset_cache_root = Path(asset_cache_root).expanduser()
    brief: dict | None = None
    script_type = "发布会"
    estimated_visuals = 0

    brief_source_count = sum(value is not None for value in (brief_path, brief_file, brief_data))
    if brief_source_count != 1:
        checks.append(CheckResult("Brief入口", "fail", "必须且只能提供一个 Brief Excel、文本文件或结构化 Brief"))
    else:
        try:
            if brief_data is not None:
                brief = dict(brief_data)
            elif brief_path:
                path = Path(brief_path).expanduser()
                if not path.is_file():
                    raise BriefError(f"文件不存在: {path}")
                briefs = read_briefs(path)
                if len(briefs) != 1:
                    raise BriefError(f"一次只能运行一个 Brief，当前有 {len(briefs)} 行")
                brief = dict(briefs[0])
            else:
                path = Path(brief_file).expanduser()
                if not path.is_file():
                    raise BriefError(f"文件不存在: {path}")
                brief = dict(read_text_brief_file(path))
            missing_values = [name for name in REQUIRED_INPUT_COLUMNS if not str(brief.get(name) or "").strip()]
            if missing_values:
                raise BriefError(f"Brief 缺少必填值: {', '.join(missing_values)}")
            script_type = normalize_script_type(brief.get("脚本类型", ""))
            brief["脚本类型"] = script_type
            duration = validate_script_brief(brief)
            checks.append(CheckResult(
                "Brief内容",
                "pass",
                f"字段完整，脚本类型为{script_type}，时长为{duration}秒",
            ))
            checks.append(CheckResult(
                "类别路由",
                "pass",
                (
                    f"{script_type} → {category_id_for_script_type(script_type)}；"
                    f"规划={route_contract_for_script_type(script_type)['planning_profile']}；"
                    "默认禁止跨类别检索"
                ),
            ))
            checks.append(CheckResult(
                "文案生成",
                "pass",
                (
                    "功能测试模式使用Brief优先本地规则，不调用文本模型"
                    if quality_profile == "draft"
                    else "客户复核模式将执行受控文本生成；模型失败会阻断，不回退为合格稿"
                ),
            ))
        except (BriefError, OSError, ValueError) as exc:
            checks.append(CheckResult("Brief内容", "fail", str(exc)))

    if loaded_asset_package is not None and brief is not None:
        expected_category_id = category_id_for_script_type(script_type)
        if loaded_asset_package.get("category_id") != expected_category_id:
            checks.append(CheckResult(
                "资产包",
                "fail",
                (
                    f"当前包 {loaded_asset_package['package_id']} 属于 "
                    f"{loaded_asset_package.get('category_id')}，不能用于 "
                    f"{script_type}/{expected_category_id}"
                ),
            ))
        else:
            try:
                source_record = asset_package_source_record(
                    asset_package,
                    expected_category_id=expected_category_id,
                )
            except AssetPackageError as exc:
                checks.append(CheckResult("资产包", "fail", str(exc)))
            else:
                source_root = loaded_asset_package["source_root_path"]
                counts = asset_package_index_summary(asset_package)
                checks.append(CheckResult(
                    "资产包",
                    "pass",
                    (
                        f"已挂载 {loaded_asset_package['package_id']} "
                        f"({expected_category_id})；本次文本与图片从包内索引读取，"
                        f"不重新解析 XLSX/图片内容；索引库记录 "
                        f"script={counts.get('script_assets', 0)}, "
                        f"media={counts.get('media_assets', 0)}, "
                        f"source-context={counts.get('media_with_source_context', 0)}, "
                        f"vision-success={counts.get('media_with_successful_analysis', 0)}, "
                        f"vision-failed={counts.get('media_with_failed_analysis', 0)}, "
                        f"vision-pending={counts.get('media_pending_analysis', 0)}, "
                        f"view={source_record['source_view']}"
                    ),
                ))

    if project_dir:
        resolved_project_dir = Path(project_dir).expanduser()
        if brief is not None:
            brief["素材目录或素材文件"] = str(resolved_project_dir)
    elif brief is not None:
        try:
            resolved_project_dir = resolve_project_media_root(brief, source_root)
        except Exception as exc:
            resolved_project_dir = None
            checks.append(CheckResult(
                "成片素材就绪",
                "warn",
                f"{exc}；不影响脚本文档生成，后续成片制作时补充",
            ))
    else:
        resolved_project_dir = None

    if resolved_project_dir is not None:
        if not resolved_project_dir.is_dir():
            checks.append(CheckResult("项目素材目录", "fail", f"目录不存在: {resolved_project_dir}"))
        else:
            image_count = 0
            video_count = 0
            for path in resolved_project_dir.rglob("*"):
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix in IMAGE_EXTENSIONS:
                    image_count += 1
                elif suffix in VIDEO_EXTENSIONS:
                    video_count += 1
            media_count = image_count + video_count
            estimated_visuals = image_count + video_count * VIDEO_FRAME_ESTIMATE
            media_summary = (
                f"图片 {image_count} 个、视频 {video_count} 个，"
                f"预计需要理解约 {estimated_visuals} 张画面"
            )
            if not media_count:
                checks.append(CheckResult(
                    "成片素材就绪",
                    "warn",
                    "目录中没有可读取的图片或视频文件；不影响脚本文档生成",
                ))
            elif _is_broad_media_root(resolved_project_dir):
                checks.append(CheckResult(
                    "项目素材目录",
                    "warn",
                    f"当前选择的是系统大目录，已禁止作为自动配图来源；脚本文档仍可生成。{media_summary}",
                ))
            elif estimated_visuals > MAX_VISUAL_ANALYSIS_COUNT:
                checks.append(CheckResult(
                    "项目素材规模",
                    "warn",
                    f"任务量超过自动视觉分析上限；脚本文档仍可生成，成片阶段再筛选素材。{media_summary}",
                ))
            elif estimated_visuals > WARN_VISUAL_ANALYSIS_COUNT:
                checks.append(CheckResult(
                    "项目素材规模",
                    "warn",
                    f"素材较多，本次运行可能较慢。{media_summary}",
                ))
            else:
                checks.append(CheckResult("项目素材目录", "pass", media_summary))

    library_path = resolve_script_type_workbook(source_root, script_type)
    template_count = creative_count = 0
    if not source_root.is_dir():
        checks.append(CheckResult("资料库目录", "fail", f"目录不存在: {source_root}"))
    elif not library_path.is_file():
        checks.append(CheckResult(f"{script_type}脚本库", "fail", f"缺少{script_type}脚本库: {library_path.name}"))
    else:
        try:
            wb = load_workbook(library_path, read_only=True, data_only=True)
            try:
                sheet_names = set(wb.sheetnames)
                missing_sheets = set(metadata_for_script_type(script_type).source_sheet_names) - sheet_names
                if missing_sheets:
                    checks.append(CheckResult(f"{script_type}脚本库", "fail", f"缺少工作表: {', '.join(sorted(missing_sheets))}"))
                else:
                    required_headers = {
                        "模版": {"结构", "镜号", "画面描述"},
                        "创意库": {"结构", "镜号", "画面描述"},
                    }
                    missing_headers = {}
                    for sheet_name, required in required_headers.items():
                        header_values = {
                            str(value).strip()
                            for row in wb[sheet_name].iter_rows(min_row=5, max_row=6, values_only=True)
                            for value in row
                            if value is not None and str(value).strip()
                        }
                        missing = required - header_values
                        if missing:
                            missing_headers[sheet_name] = ", ".join(sorted(missing))
                    if missing_headers:
                        details = "；".join(f"{name}缺少{fields}" for name, fields in missing_headers.items())
                        checks.append(CheckResult(f"{script_type}脚本库", "fail", details))
                    else:
                        template_count = len(load_template_slots(
                            source_root,
                            script_type=script_type,
                            workbook_path=library_path,
                        ))
                        creative_count = len(load_creative_variants(
                            source_root,
                            script_type=script_type,
                            workbook_path=library_path,
                        ))
                        if not template_count_valid(template_count):
                            checks.append(CheckResult("模板槽位", "fail", f"没有可读取的{script_type}模板槽位"))
                        elif script_type == "发布会" and not template_count_recommended(template_count):
                            checks.append(CheckResult(
                                "模板槽位", "warn",
                                f"已读取{template_count}个{script_type}模板槽位；建议范围为12到18个",
                            ))
                        else:
                            checks.append(CheckResult("模板槽位", "pass", f"已读取{template_count}个{script_type}模板槽位"))
                        if creative_count == 0:
                            checks.append(CheckResult("创意候选", "fail", "创意库没有可读取的有效镜头"))
                        else:
                            checks.append(CheckResult("创意候选", "pass", f"已读取{creative_count}个创意候选"))
            finally:
                wb.close()
        except Exception as exc:
            checks.append(CheckResult(f"{script_type}脚本库", "fail", f"读取失败: {exc}"))

    if brief is not None:
        provider_ready = minimax_credentials_available(api_key or "", minimax_command)
        if quality_profile == "client-review":
            routes = [("初稿", writer_provider), ("编辑", editor_provider)]
            route_results = []
            for label, route in routes:
                ready = (
                    provider_ready
                    if str(route).casefold() in {"minimax", "minimax-cli", "mmx"}
                    else provider_credentials_available(route, command=minimax_command)
                )
                route_results.append(ready)
                checks.append(CheckResult(
                    f"{label}模型凭据",
                    "pass" if ready else "fail",
                    f"{route} 已就绪" if ready else f"{route} 缺少凭据或可执行命令",
                ))
            checks.append(CheckResult(
                "文本模型凭据",
                "pass" if all(route_results) else "fail",
                "写作与编辑模型均已就绪" if all(route_results) else "至少一个文本模型缺少凭据；禁止回退为合格稿",
            ))
        cache_dir = project_cache_dir(brief, asset_cache_root)
        analysis_path = cache_dir / "image_analysis.minimax.jsonl"
        cached_count = _count_jsonl_records(analysis_path)
        estimated_new_calls = max(0, estimated_visuals - cached_count)
        if provider_ready:
            checks.append(CheckResult("视觉AI凭据", "pass", "MiniMax凭据或登录状态可用"))
        else:
            if allow_cached_analysis and analysis_path.is_file() and analysis_path.stat().st_size > 0:
                checks.append(CheckResult("视觉AI凭据", "warn", "未检测到新凭据，将显式复用已有视觉分析缓存"))
            else:
                checks.append(CheckResult(
                    "视觉AI凭据",
                    "warn",
                    "未检测到MINIMAX_API_KEY或可用mmx登录；将跳过可选视觉分析，不影响脚本文档生成",
                ))
        call_status: CheckStatus = "pass" if estimated_new_calls == 0 else "warn"
        checks.append(CheckResult(
            "视觉AI预计调用",
            call_status,
            f"已有缓存约 {cached_count} 条；本次预计新增 {estimated_new_calls} 次视觉分析"
            + ("，不会新增调用" if estimated_new_calls == 0 else "，开始生成前请确认素材范围"),
        ))

    output_check = _check_output_directory(output_dir, prepare=prepare_output)
    checks.append(output_check)

    return PreflightReport(
        brief=brief,
        project_dir=resolved_project_dir,
        output_dir=output_dir,
        checks=tuple(checks),
    )


def inspect_launch_preflight(**kwargs) -> PreflightReport:
    """只读预检；不创建输出目录或任何运行产物。"""
    return run_launch_preflight(**kwargs, prepare_output=False)


def _check_output_directory(output_dir: Path, *, prepare: bool) -> CheckResult:
    try:
        if prepare:
            output_dir.mkdir(parents=True, exist_ok=True)
            writable_root = output_dir
        elif output_dir.exists():
            if not output_dir.is_dir():
                raise OSError("目标已存在但不是目录")
            writable_root = output_dir
        else:
            writable_root = output_dir.parent
            while not writable_root.exists() and writable_root != writable_root.parent:
                writable_root = writable_root.parent
            if not writable_root.is_dir():
                raise OSError("找不到可用的父目录")
        if not os.access(writable_root, os.W_OK):
            raise OSError("目录不可写")
        mode = "将于执行时创建" if not output_dir.exists() else "可写"
        return CheckResult("输出目录", "pass", f"{output_dir}（{mode}）")
    except OSError as exc:
        return CheckResult("输出目录", "fail", f"{output_dir}: {exc}")


def _is_broad_media_root(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    home = Path.home().resolve()
    broad_roots = {
        home,
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "Movies",
        home / "Pictures",
    }
    return resolved in broad_roots


def _count_jsonl_records(path: Path) -> int:
    if not path.is_file():
        return 0
    count = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            if isinstance(json.loads(line), dict):
                count += 1
    except (OSError, json.JSONDecodeError):
        return 0
    return count
