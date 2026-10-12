from __future__ import annotations

import argparse
import base64
from concurrent.futures import Future
from copy import deepcopy
import hashlib
from io import BytesIO
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Literal, Optional
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener
import uuid
import webbrowser

from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .action_confirmation import ActionConfirmationService
from .artifact_service import ArtifactResolutionError, RunArtifactResolver
from .auto_pipeline import project_cache_dir
from .asset_package import AssetPackageError, asset_package_index_summary
from .asset_center_tools import (
    AssetCenterToolService,
    AssetToolRecoveryRequired,
)
from .brief_artifact_store import BriefArtifactStore, load_or_create_confirmation_key
from .cli import (
    DEFAULT_ASSET_CACHE_ROOT,
    DEFAULT_ASSET_PACKAGE,
    DEFAULT_OUTPUT_ROOT as CONFIGURED_OUTPUT_ROOT,
    DEFAULT_SOURCE_ROOT,
)
from .creative_strategy_tools import (
    CreativeStrategyRecoveryRequired,
    CreativeStrategyToolService,
)
from .excel_io import read_briefs, read_text_brief_file
from .evolution import summarize_evolution_ledgers
from .executable_agent import ExecutableAgentService, ExecutableAgentServices
from .formal_agent import FormalExecutableAgentService
from .formal_brief_input_adapter import FormalBriefInputAdapter
from .formal_brief_domain import FormalBriefDomainError
from .formal_console import FormalBriefUploadFactory, install_formal_routes
from .formal_generation_service import FormalGenerationService
from .formal_admin_service import FormalAdminService
from .formal_script_workspace import FormalScriptWorkspaceService
from .formal_image_workspace import FormalImageWorkspaceService
from .formal_review_workspace import FormalReviewWorkspaceService
from .image_understanding import minimax_credentials_available
from .identity_service import LocalIdentityAdapter, require_permission
from .project_service import ProjectService
from .knowledge_tools import KnowledgeToolRecoveryRequired, KnowledgeToolService
from .preflight import CheckResult, PreflightReport, run_launch_preflight
from .readonly_agent import ReadonlyAgentService
from .review_package import (
    ReviewPackageError,
    export_review_workbook,
    validate_review_workbook,
)
from .review_quality import ReviewWorkbookError, analyze_review_workbooks
from .runtime_config import (
    configured_profile,
    configured_source_commit,
    default_profile_port,
    legacy_roots,
    load_local_secrets,
    load_runtime_config,
    remap_legacy_path,
)
from .run_contract import sha256_file, sha256_json
from .run_state import write_json_atomic
from .revision_service import (
    build_revision_report,
    compare_versions,
    version_shots_from_artifacts,
)
from .stage_contract import STAGE_ORDER
from .user_feedback import load_feedback_status, record_feedback_status, store_user_feedback
from .visual_candidates import VisualCandidateError, candidates_for_run, find_run_dir
from .visual_review import write_visual_review_html_from_workbook
from .workspace_store import WorkspaceServiceError, WorkspaceStore
from .version_service import VersionService
from .version_publication_projection import VersionPublicationProjectionService
from .version_delivery import VersionDeliveryContext, VersionDeliveryService
from .tool_idempotency import ToolIdempotencyService
from .ui_workbench import UiWorkbenchService, UiWorkbenchServices
from .ui_governance import UiGovernanceService, UiGovernanceServices
from .version_workbench import (
    VersionWorkbenchRecoveryRequired,
    VersionWorkbenchService,
)
from .manual_version_commands import ManualVersionCommandService, ManualVersionCommandServices
from .manual_asset_commands import ManualAssetPublishCommandService, ManualAssetPublishCommandServices
from .manual_knowledge_commands import ManualKnowledgeApprovalCommandService, ManualKnowledgeApprovalCommandServices
from .knowledge_service import KnowledgeService
from .script_type_adapter import (
    SCRIPT_TYPE_METADATA,
    category_id_for_script_type,
    normalize_script_type,
    resolve_script_type_workbook,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = Path(__file__).resolve().parent / "ui"
DEFAULT_OUTPUT_ROOT = CONFIGURED_OUTPUT_ROOT


@dataclass(frozen=True)
class UiNetworkSettings:
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: frozenset[str]
    public_url: str
    auth_mode: str


def load_ui_network_settings(environ: dict | None = None) -> UiNetworkSettings:
    environment = os.environ if environ is None else environ
    host = str(environment.get("MPLUS_HOST") or "127.0.0.1").strip()
    if not host or any(character in host for character in "/?#"):
        raise ValueError("MPLUS_HOST 无效")
    try:
        port = int(str(
            environment.get("MPLUS_PORT")
            or default_profile_port(configured_profile(environment))
        ))
    except ValueError as exc:
        raise ValueError("MPLUS_PORT 必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("MPLUS_PORT 必须在 1..65535")

    external_bind = host in {"0.0.0.0", "::"} or host not in {"127.0.0.1", "localhost", "::1"}
    allowed_hosts_raw = str(environment.get("MPLUS_ALLOWED_HOSTS") or "").strip()
    allowed_origins_raw = str(environment.get("MPLUS_ALLOWED_ORIGINS") or "").strip()
    public_url_raw = str(environment.get("MPLUS_PUBLIC_URL") or "").strip()
    if external_bind and not all((allowed_hosts_raw, allowed_origins_raw, public_url_raw)):
        raise ValueError(
            "非本机监听必须同时配置 MPLUS_ALLOWED_HOSTS、"
            "MPLUS_ALLOWED_ORIGINS 和 MPLUS_PUBLIC_URL"
        )

    allowed_hosts = tuple(
        item.strip() for item in allowed_hosts_raw.split(",") if item.strip()
    ) or ("127.0.0.1", "localhost", "testserver")
    if any(item == "*" or "/" in item or "://" in item for item in allowed_hosts):
        raise ValueError("MPLUS_ALLOWED_HOSTS 只接受明确主机名，不接受通配符或 URL")

    allowed_origins = frozenset(
        _validated_origin(item.strip())
        for item in allowed_origins_raw.split(",")
        if item.strip()
    ) or frozenset({
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        "http://testserver",
    })
    public_url = _validated_origin(
        public_url_raw or f"http://{host}:{port}"
    )
    if external_bind and public_url not in allowed_origins:
        raise ValueError("MPLUS_PUBLIC_URL 必须包含在 MPLUS_ALLOWED_ORIGINS 中")
    auth_mode = str(environment.get("MPLUS_AUTH_MODE") or "local").strip().casefold()
    if auth_mode not in {"local", "github"}:
        raise ValueError("MPLUS_AUTH_MODE 只能是 local 或 github")
    if external_bind:
        if public_url.split(":", 1)[0] != "https":
            raise ValueError("非本机监听的 MPLUS_PUBLIC_URL 必须使用 HTTPS")
        required_oauth = (
            "MPLUS_GITHUB_CLIENT_ID",
            "MPLUS_GITHUB_CLIENT_SECRET",
            "MPLUS_GITHUB_SESSION_SECRET",
            "MPLUS_POSTGRES_DSN",
        )
        missing_oauth = [name for name in required_oauth if not str(environment.get(name) or "").strip()]
        if not any(
            str(environment.get(name) or "").strip()
            for name in ("MPLUS_GITHUB_ALLOW_USERS", "MPLUS_GITHUB_ALLOW_ORGS")
        ):
            missing_oauth.append("MPLUS_GITHUB_ALLOW_USERS/MPLUS_GITHUB_ALLOW_ORGS")
        if auth_mode != "github" or missing_oauth:
            missing = "、".join(missing_oauth) or "MPLUS_AUTH_MODE=github"
            raise ValueError(f"非本机监听必须启用完整 GitHub OAuth: {missing}")
        if len(str(environment["MPLUS_GITHUB_SESSION_SECRET"])) < 32:
            raise ValueError("MPLUS_GITHUB_SESSION_SECRET 不得少于 32 字符")
    return UiNetworkSettings(
        host=host,
        port=port,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        public_url=public_url,
        auth_mode=auth_mode,
    )


def _validated_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Origin 必须是无路径、查询和凭据的 http(s) 地址")
    return f"{parsed.scheme}://{parsed.netloc}"


_NETWORK = load_ui_network_settings()
HOST = _NETWORK.host
PORT = _NETWORK.port
ALLOWED_HOSTS = _NETWORK.allowed_hosts
ALLOWED_ORIGINS = _NETWORK.allowed_origins
PUBLIC_URL = _NETWORK.public_url
AUTH_MODE = _NETWORK.auth_mode
TOKEN_HEADER = "X-MPlus-UI-Token"
TERMINAL_STATES = {
    "machine_blocked",
    "waiting_review",
    "review_invalid",
    "complete",
    "failed",
    "cancelled",
}


def validated_formal_agent_header(agent_api, path: str, auth_token: str) -> bool:
    """Allow GitHub-mode API bypass only for an existing scoped local session."""
    if not path.startswith("/api/formal/") or path == "/api/formal/owner":
        return False
    token = str(auth_token or "").strip()
    if not token:
        return False
    try:
        agent_api.services.identity.validate_session(token)
    except (AttributeError, WorkspaceServiceError):
        return False
    return True

BRIEF_PRESETS = {
    "发布会": {
        "project_name": "澄域科技新品发布会开场片", "output_filename": "澄域科技新品发布会开场片-发布会脚本.xlsx",
        "script_type": "发布会", "duration_seconds": 60, "client": "澄域科技", "industry": "消费科技发布会",
        "tone": "冷峻、纯净、科技感，节奏由克制铺陈推进至发布高潮",
        "required_content": "品牌 Logo、主会场、核心产品、发布仪式、品牌落版",
        "forbidden_content": "不出现其他品牌；不虚构产品参数、嘉宾身份或主题名",
        "material_path": "", "note": "测试用 Brief；未知项目事实统一标记待确认",
    },
    "展会": {
        "project_name": "衡岚循环材料未来展", "output_filename": "衡岚循环材料未来展-展会脚本.xlsx",
        "script_type": "展会", "duration_seconds": 90, "client": "衡岚材料", "industry": "循环材料主题展",
        "tone": "清晰、前沿、有空间探索感，兼顾展项信息与观众体验",
        "required_content": "品牌展台、入口、核心产品、观众互动、品牌落版",
        "forbidden_content": "不出现其他展会品牌；不虚构材料性能、合作单位或数据",
        "material_path": "", "note": "测试用 Brief；具体展项名称未知时标记待确认",
    },
    "年会": {
        "project_name": "星澜共创年度盛典", "output_filename": "星澜共创年度盛典-年会脚本.xlsx",
        "script_type": "年会", "duration_seconds": 120, "client": "星澜集团", "industry": "企业年度盛典",
        "tone": "温暖、昂扬、有团队凝聚力，情绪逐步推向年度荣誉时刻",
        "required_content": "企业 Logo、员工入场、高管致辞、团队颁奖、全员合影",
        "forbidden_content": "不出现其他公司；不虚构业绩数字、奖项名称或领导身份",
        "material_path": "", "note": "测试用 Brief；真实业绩和人员信息统一待确认",
    },
    "赛事": {
        "project_name": "逐峰城市挑战赛", "output_filename": "逐峰城市挑战赛-赛事脚本.xlsx",
        "script_type": "赛事", "duration_seconds": 90, "client": "逐峰运动", "industry": "城市综合挑战赛",
        "tone": "速度、力量、真实竞技感，剪辑紧凑但保留关键赛况",
        "required_content": "赛事标识、邀请函入场、起跑集结、竞技瞬间、冲线或颁奖",
        "forbidden_content": "不出现其他赛事品牌；不虚构成绩、赛制、选手姓名或名次",
        "material_path": "", "note": "测试用 Brief；具体成绩与选手信息标记待确认",
    },
    "音乐节": {
        "project_name": "潮汐声场海岸音乐节", "output_filename": "潮汐声场海岸音乐节-音乐节脚本.xlsx",
        "script_type": "音乐节", "duration_seconds": 75, "client": "潮汐声场", "industry": "户外音乐节",
        "tone": "年轻、自由、强节拍，兼顾舞台爆发与观众情绪",
        "required_content": "活动主视觉、活动场地、舞台表演、观众互动、品牌落版",
        "forbidden_content": "不出现其他音乐节品牌；不虚构艺人阵容、曲目或演出时间",
        "material_path": "", "note": "测试用 Brief；艺人与日程信息未知时标记待确认",
    },
    "明星活动": {
        "project_name": "澜序品牌明星活动", "output_filename": "澜序品牌明星活动-明星活动脚本.xlsx",
        "script_type": "明星活动", "duration_seconds": 90, "client": "澜序品牌", "industry": "品牌明星活动",
        "tone": "高级、克制、具有现场关注度，突出人物出场与品牌关系",
        "required_content": "品牌 Logo、活动空间、明星出场、产品互动、品牌落版",
        "forbidden_content": "不出现其他品牌；不虚构明星身份、代言关系、发言或产品信息",
        "material_path": "", "note": "测试用 Brief；明星与合作身份统一标记待确认",
    },
}


class BriefForm(BaseModel):
    project_name: str = Field(min_length=1, max_length=120)
    output_filename: str = Field(default="", max_length=180)
    script_type: str = "发布会"
    duration_seconds: int = Field(default=60, ge=1, le=3600)
    client: str = Field(default="", max_length=120)
    industry: str = Field(default="", max_length=120)
    tone: str = Field(min_length=1, max_length=1000)
    required_content: str = Field(min_length=1, max_length=2000)
    forbidden_content: str = Field(default="", max_length=2000)
    material_path: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=2000)

    @field_validator("script_type")
    @classmethod
    def known_script_type(cls, value: str) -> str:
        return normalize_script_type(value)

    def to_brief(self) -> dict:
        script_type = normalize_script_type(self.script_type)
        output_filename = self.output_filename.strip() or f"{self.project_name.strip()}-{script_type}脚本.xlsx"
        return {
            "项目名称": self.project_name.strip(),
            "项目区域文件名": output_filename,
            "脚本类型": script_type,
            "品牌或客户": self.client.strip(),
            "行业或活动类别": self.industry.strip(),
            "视频时长": f"{self.duration_seconds}s",
            "风格调性": self.tone.strip(),
            "必须出现的内容": self.required_content.strip(),
            "禁忌或不能出现的内容": self.forbidden_content.strip(),
            "素材目录或素材文件": self.material_path.strip(),
            "备注": self.note.strip(),
        }


class ControlledBriefForm(BriefForm):
    """Internal E06 handoff; browser-created LaunchPayloads stay on BriefForm."""

    planning_events: list[dict] = Field(default_factory=list, max_length=500)
    creative_strategy_selection: dict = Field(default_factory=dict)

    def to_brief(self) -> dict:
        brief = super().to_brief()
        if self.planning_events:
            brief["planning_events"] = self.planning_events
        if self.creative_strategy_selection:
            brief["creative_strategy_selection"] = self.creative_strategy_selection
        return brief


class LaunchOptions(BaseModel):
    source_root: str = str(DEFAULT_SOURCE_ROOT)
    asset_package: str = Field(default="", max_length=4000)
    output_dir: str = str(DEFAULT_OUTPUT_ROOT)
    max_loops: int = Field(default=3, ge=1, le=8)
    quality_profile: Literal["draft", "client-review"] = "draft"
    text_model: str = Field(default="MiniMax-M2.7", min_length=1, max_length=120)
    model_profile: Literal[
        "adaptive",
        "minimax-m3",
        "deepseek-only",
        "current",
        "dual-ab",
    ] = "adaptive"
    writer_provider: Literal["minimax-cli", "deepseek-http"] = "minimax-cli"
    writer_model: str = Field(default="MiniMax-M2.7", min_length=1, max_length=120)
    editor_provider: Literal["minimax-cli", "deepseek-http"] = "minimax-cli"
    editor_model: str = Field(default="MiniMax-M2.7", min_length=1, max_length=120)
    vision_model: str = Field(default="", max_length=120)
    allow_cached_analysis: bool = False
    resume_run_id: str = Field(default="", max_length=180)
    from_stage: str = Field(default="", max_length=80)
    retry_stage: str = Field(default="", max_length=80)
    revision_constraints: str = Field(default="", max_length=4000)
    parent_run_id: str = Field(default="", max_length=180)
    review_workbook: str = Field(default="", max_length=4000)


class LaunchPayload(BaseModel):
    brief: BriefForm
    options: LaunchOptions = Field(default_factory=LaunchOptions)


class PathPayload(BaseModel):
    path: str = Field(min_length=1, max_length=4000)


class FeedbackPayload(BaseModel):
    category: Literal["image", "style", "detail", "copy", "brand", "fact", "structure", "technical"]
    scope: Literal["whole_film", "shot", "adjacent"] = "whole_film"
    shot_no: str = Field(default="", max_length=40)
    severity: Literal["normal", "important", "blocking"] = "normal"
    comment: str = Field(min_length=1, max_length=4000)
    desired_change: str = Field(default="", max_length=4000)
    keep_fields: list[str] = Field(default_factory=list, max_length=20)
    selected_image_id: str = Field(default="", max_length=180)
    selected_image_path: str = Field(default="", max_length=4000)
    supplemental_image_path: str = Field(default="", max_length=4000)
    approve_text_only: bool = False


class CandidateQuery(BaseModel):
    shot_no: str = Field(min_length=1, max_length=40)
    query: str = Field(default="", max_length=1000)


class AgentOwnerEnablePayload(BaseModel):
    workspace_name: str = Field(default="Loomtake 本地工作区", min_length=1, max_length=120)
    display_name: str = Field(default="本机管理员", min_length=1, max_length=120)
    confirmed: bool = False


class AgentOwnerRecoveryPayload(BaseModel):
    confirmed: bool = False


class AgentSessionPayload(BaseModel):
    project_id: str = Field(min_length=8, max_length=128)


class WorkbenchContextPayload(BaseModel):
    model_config = {"extra": "forbid"}
    stage: Literal['creative', 'formal']
    scope: Literal['current', 'selected', 'all']
    current_target_id: str = Field(default='', max_length=160)
    selected_target_ids: list[str] = Field(default_factory=list, max_length=240)
    expected_project_version: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    expected_progress_revision: int | None = Field(default=None, ge=0)


class WorkbenchConfirmPayload(BaseModel):
    model_config = {"extra": "forbid"}
    proposal_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    retained_group_ids: list[str] = Field(max_length=240)


class AgentMessagePayload(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    run_id: Optional[str] = Field(default=None, min_length=8, max_length=128)
    context: WorkbenchContextPayload | None = None
    operation_key: str | None = Field(default=None, min_length=8, max_length=160)


class BriefAnalysisPayload(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class BriefQuestionAnswerItem(BaseModel):
    model_config = {"extra": "forbid"}
    question_id: str = Field(min_length=1, max_length=180)
    analysis_task_id: str = Field(min_length=8, max_length=160)
    content: str = Field(min_length=1, max_length=4000)


class BriefQuestionAnswerPayload(BaseModel):
    model_config = {"extra": "forbid"}
    question_id: str = Field(default="", max_length=180)
    analysis_task_id: str = Field(default="", max_length=160)
    content: str = Field(default="", max_length=4000)
    answers: list[BriefQuestionAnswerItem] | None = Field(default=None, max_length=20)
    idempotency_key: str = Field(default="", max_length=160)
    expected_project_version: int = Field(ge=0)


class AgentConfirmPayload(BaseModel):
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentImageSelectionPayload(BaseModel):
    candidate_key: str = Field(pattern=r"^candidate_[0-9a-f]{24}$")
    reuse_reason: str = Field(default="", max_length=1000)


class AgentTemporaryTextBasisPayload(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class AgentOnboardProjectPayload(BaseModel):
    workspace_id: str = Field(min_length=8, max_length=128)
    expected_workspace_version: int = Field(default=0, ge=0)
    brief: BriefForm
    confirmed: bool = False


class WorkbenchVersionComparePayload(BaseModel):
    project_id: str = Field(min_length=8, max_length=128)
    left_version_id: str = Field(min_length=8, max_length=128)
    right_version_id: str = Field(min_length=8, max_length=128)


class ManualVersionActionPayload(BaseModel):
    tool_name: Literal[
        "record_version_review",
        "select_candidate_version",
        "publish_version",
    ]
    parameters: dict
    command_token: str = Field(default="", max_length=2048)


class WorkbenchAssetFreezePayload(BaseModel):
    agent_session_id: str = Field(min_length=8, max_length=128)
    path: str = Field(min_length=1, max_length=4000)


class WorkbenchAssetActionPayload(BaseModel):
    agent_session_id: str = Field(min_length=8, max_length=128)
    tool_name: Literal[
        "create_asset_import",
        "review_asset_import",
    ]
    parameters: dict
    confirmation_token: str = Field(default="", max_length=2048)


class ManualAssetPublishPreparePayload(BaseModel):
    project_id: str = Field(min_length=8, max_length=128)
    package_id: str = Field(min_length=8, max_length=128)
    expected_active_package_version: int = Field(ge=0)
    import_id: str = Field(min_length=8, max_length=128)
    expected_import_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=160)


class WorkbenchAssetSearchPayload(BaseModel):
    agent_session_id: str = Field(min_length=8, max_length=128)
    category_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    query: str = Field(min_length=1, max_length=1000)
    kind: Literal["all", "script", "image", "workbook"] = "all"


class GovernanceStrategyAnalyzePayload(BaseModel):
    agent_session_id: str = Field(min_length=8, max_length=128)


class GovernanceStrategyPreparePayload(BaseModel):
    agent_session_id: str = Field(min_length=8, max_length=128)
    strategy_portfolio_ref: str = Field(min_length=24, max_length=2048)
    strategy_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    idempotency_key: str = Field(min_length=8, max_length=160)


class GovernanceActionExecutePayload(BaseModel):
    agent_session_id: str = Field(min_length=8, max_length=128)
    parameters: dict
    confirmation_token: str = Field(min_length=24, max_length=2048)


class AdminConnectionCheckPayload(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["text", "vision"]
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,160}$")


class GovernanceKnowledgeCandidatePayload(BaseModel):
    agent_session_id: str = Field(min_length=8, max_length=128)
    scope: Literal["company", "category", "project", "personal"] = "project"
    statement: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=160)


class GovernanceKnowledgeApprovalPayload(BaseModel):
    project_id: str = Field(min_length=8, max_length=128)
    candidate_id: str = Field(min_length=8, max_length=128)
    review_evidence: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=8, max_length=160)


class ManualCommandExecutePayload(BaseModel):
    project_id: str = Field(min_length=8, max_length=128)
    parameters: dict
    command_token: str = Field(min_length=24, max_length=2048)


class CloudInputFilePayload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    storage_ref: str = Field(min_length=16, max_length=4000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=500 * 1024 * 1024)
    role: str = Field(default="attachment", min_length=1, max_length=80)


class CloudRunPayload(BaseModel):
    project_id: str = Field(min_length=8, max_length=128)
    category_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    package_ref: str = Field(min_length=3, max_length=180)
    input_ref: str = Field(min_length=16, max_length=4000)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_filename: str = Field(min_length=1, max_length=255)
    input_size_bytes: int = Field(default=0, ge=0, le=500 * 1024 * 1024)
    input_files: list[CloudInputFilePayload] = Field(default_factory=list, max_length=2000)
    runner_version: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=16, max_length=180)
    invocation_source: Literal["cli", "api", "ui"] = "api"
    parent_run_id: str | None = Field(default=None, max_length=180)
    source_run_id: str | None = Field(default=None, max_length=180)
    request: dict = Field(default_factory=dict)


@dataclass
class Artifact:
    artifact_id: str
    path: Path | None
    kind: str
    label: str
    filename: str
    size_bytes: int
    storage_ref: str = ""
    sha256: str = ""


class HistoricalContinuePayload(BaseModel):
    action: Literal["current_project", "new_project"]
    idempotency_key: str = Field(min_length=1, max_length=160)


class HistoricalConfirmPayload(BaseModel):
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class AdminRecoveryProposalPayload(BaseModel):
    action: Literal["retry_current_stage", "resume_checkpoint", "safe_reset"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_project_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=160)
    checkpoint_id: str = Field(default="", max_length=160)


class AdminRecoveryConfirmPayload(BaseModel):
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactRegistry:
    def __init__(self, *, cloud_storage=None, cache_root: str | Path | None = None) -> None:
        self._items: dict[str, Artifact] = {}
        self._identity_ids: dict[tuple[object, ...], str] = {}
        self._lock = threading.Lock()
        self.cloud_storage = cloud_storage
        self.cache_root = Path(cache_root).expanduser().resolve() if cache_root else None

    def register(self, path: str | Path | None, kind: str, label: str) -> dict | None:
        if not path:
            return None
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            return None
        digest = sha256_file(resolved)
        size_bytes = resolved.stat().st_size
        identity = ("local", str(resolved), kind, label, digest, size_bytes)
        with self._lock:
            artifact_id = self._identity_ids.get(identity, "")
            if not artifact_id:
                artifact_id = secrets.token_urlsafe(18)
                self._identity_ids[identity] = artifact_id
                self._items[artifact_id] = Artifact(
                    artifact_id=artifact_id,
                    path=resolved,
                    kind=kind,
                    label=label,
                    filename=resolved.name,
                    size_bytes=size_bytes,
                    sha256=digest,
                )
        return {
            "id": artifact_id,
            "kind": kind,
            "label": label,
            "filename": resolved.name,
            "size_bytes": size_bytes,
        }

    def register_remote(
        self,
        storage_ref: str,
        sha256: str,
        *,
        kind: str,
        label: str,
        filename: str,
        size_bytes: int,
    ) -> dict | None:
        if not self.cloud_storage or not storage_ref:
            return None
        identity = (
            "remote", storage_ref, sha256, kind, label, filename, int(size_bytes),
        )
        with self._lock:
            artifact_id = self._identity_ids.get(identity, "")
            if not artifact_id:
                artifact_id = secrets.token_urlsafe(18)
                self._identity_ids[identity] = artifact_id
                self._items[artifact_id] = Artifact(
                    artifact_id=artifact_id,
                    path=None,
                    kind=kind,
                    label=label,
                    filename=filename,
                    size_bytes=int(size_bytes),
                    storage_ref=storage_ref,
                    sha256=sha256,
                )
        return {
            "id": artifact_id,
            "kind": kind,
            "label": label,
            "filename": filename,
            "size_bytes": int(size_bytes),
        }

    def get(self, artifact_id: str) -> Artifact:
        with self._lock:
            artifact = self._items.get(artifact_id)
        if not artifact:
            raise KeyError(artifact_id)
        if artifact.path is not None:
            if not artifact.path.is_file() or sha256_file(artifact.path) != artifact.sha256:
                raise KeyError(artifact_id)
        return artifact

    def materialize(self, artifact_id: str) -> Path:
        artifact = self.get(artifact_id)
        if artifact.path is not None:
            return artifact.path
        if not self.cloud_storage or self.cache_root is None:
            raise KeyError(artifact_id)
        target = self.cache_root / artifact.sha256 / artifact.filename
        return self.cloud_storage.download_file(
            artifact.storage_ref,
            target,
            expected_sha256=artifact.sha256,
        )


@dataclass
class Job:
    job_id: str
    payload: LaunchPayload
    state: str = "queued"
    message: str = "等待启动"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    return_code: int | None = None
    logs: list[str] = field(default_factory=list)
    report: dict | None = None
    artifacts: list[dict] = field(default_factory=list)
    process: subprocess.Popen | None = None
    cancel_requested: bool = False
    cache_dir: Path | None = None
    manifest_path: Path | None = None
    last_output_at: float | None = None

    def snapshot(self) -> dict:
        now = self.finished_at or time.time()
        started = self.started_at or self.created_at
        live_report = self.report or _live_manifest_report(self)
        stages, active_stage = read_stage_progress(live_report)
        process_alive = bool(self.process and self.process.poll() is None)
        last_activity = self.last_output_at or self.started_at or self.created_at
        return {
            "job_id": self.job_id,
            "state": self.state,
            "message": self.message,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": max(0, round(now - started)),
            "return_code": self.return_code,
            "logs": self.logs[-120:],
            "report": self.report,
            "artifacts": self.artifacts,
            "stages": stages,
            "active_stage": active_stage,
            "progress": stage_progress_summary(stages),
            "model_status": model_status_summary(self.payload.options, stages, active_stage),
            "feedback": load_feedback_status(self.payload.options.revision_constraints),
            "run_id": _run_id_from_report(live_report),
            "heartbeat": {
                "process_alive": process_alive,
                "checked_at": time.time(),
                "seconds_since_output": max(0, round(time.time() - last_activity)),
                "manifest_updated_at": (
                    self.manifest_path.stat().st_mtime
                    if self.manifest_path and self.manifest_path.is_file()
                    else None
                ),
            },
        }


class JobManager:
    def __init__(self, artifacts: ArtifactRegistry) -> None:
        self.artifacts = artifacts
        self._jobs: dict[str, Job] = {}
        self._active_job_id: str | None = None
        self._lock = threading.Lock()

    def start(self, payload: LaunchPayload) -> Job:
        options = payload.options
        if options.from_stage and options.retry_stage:
            raise HTTPException(status_code=422, detail="from_stage 与 retry_stage 不能同时使用")
        if options.retry_stage and not options.resume_run_id:
            raise HTTPException(status_code=422, detail="retry_stage 必须指定 resume_run_id")
        if options.from_stage and not (options.resume_run_id or options.parent_run_id):
            raise HTTPException(status_code=422, detail="from_stage 必须指定 resume_run_id 或 parent_run_id")
        if options.resume_run_id and options.parent_run_id:
            raise HTTPException(status_code=422, detail="resume_run_id 与 parent_run_id 不能同时使用")
        if options.review_workbook and not options.resume_run_id:
            raise HTTPException(status_code=422, detail="导入人工复核表必须指定 resume_run_id")
        if options.review_workbook and (options.from_stage or options.retry_stage):
            raise HTTPException(status_code=422, detail="导入人工复核表时不能同时指定节点重跑")
        valid_stages = {stage.value for stage in STAGE_ORDER}
        if options.from_stage and options.from_stage not in valid_stages:
            raise HTTPException(status_code=422, detail="from_stage 不是有效节点")
        if options.retry_stage and options.retry_stage not in valid_stages:
            raise HTTPException(status_code=422, detail="retry_stage 不是有效节点")
        report = preflight_payload(payload)
        if not report.passed:
            raise HTTPException(status_code=422, detail=preflight_response(report))
        with self._lock:
            if self._active_job_id:
                active = self._jobs.get(self._active_job_id)
                if active and active.state not in TERMINAL_STATES:
                    raise HTTPException(status_code=409, detail="当前已有脚本任务正在生成")
            job = Job(job_id=uuid.uuid4().hex, payload=payload)
            self._jobs[job.job_id] = job
            self._active_job_id = job.job_id
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.state in TERMINAL_STATES:
            return job
        job.cancel_requested = True
        job.state = "cancelling"
        job.message = "正在停止当前运行"
        process = job.process
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return job

    def _run(self, job: Job) -> None:
        job.started_at = time.time()
        job.state = "running"
        job.message = f"正在按{job.payload.brief.script_type}模板槽位生成脚本"
        brief = job.payload.brief.to_brief()
        options = job.payload.options
        if options.revision_constraints:
            _record_job_feedback_status(
                job,
                options.revision_constraints,
                "processing",
                child_job_id=job.job_id,
            )
        writer_provider, writer_model, editor_provider, editor_model, vision_model = resolve_model_route(options)
        output_dir = Path(options.output_dir).expanduser().resolve()
        input_dir = output_dir / ".mplus-ui-inputs"
        input_dir.mkdir(parents=True, exist_ok=True)
        brief_path = input_dir / f"{job.job_id}-brief.txt"
        brief_path.write_text(serialize_brief(brief), encoding="utf-8")
        cache_dir = project_cache_dir(brief, DEFAULT_ASSET_CACHE_ROOT)
        job.cache_dir = cache_dir
        existing_reports = {path.resolve() for path in cache_dir.glob("runs/*/run_report.json")}
        command = [
            sys.executable,
            "-m",
            "mvp_v2.launch_entry",
            "--brief-file",
            str(brief_path),
            "--source-root",
            str(Path(options.source_root).expanduser()),
            "--output-dir",
            str(output_dir),
            "--asset-cache-root",
            str(DEFAULT_ASSET_CACHE_ROOT),
            "--max-loops",
            str(options.max_loops),
            "--quality-profile",
            options.quality_profile,
            "--text-model",
            options.text_model,
            "--writer-provider", writer_provider,
            "--writer-model", writer_model,
            "--editor-provider", editor_provider,
            "--editor-model", editor_model,
            "--vision-model", vision_model,
        ]
        if options.asset_package.strip():
            command.extend(["--asset-package", options.asset_package.strip()])
        if job.payload.brief.material_path.strip():
            command.extend(["--project-dir", job.payload.brief.material_path.strip()])
        if options.allow_cached_analysis:
            command.append("--allow-cached-analysis")
        if options.resume_run_id:
            command.extend(["--resume", options.resume_run_id])
        if options.from_stage:
            command.extend(["--from-stage", options.from_stage])
        if options.retry_stage:
            command.extend(["--retry-stage", options.retry_stage])
        if options.revision_constraints:
            command.extend(["--revision-constraints", options.revision_constraints])
        if options.parent_run_id:
            command.extend(["--parent-run-id", options.parent_run_id])
        if options.review_workbook:
            command.extend(["--review-workbook", options.review_workbook])
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        try:
            if job.cancel_requested:
                self._finish_cancelled(job)
                return
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            job.process = process
            if job.cancel_requested and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            if process.stdout:
                for line in process.stdout:
                    text = line.rstrip()
                    if text:
                        job.logs.append(text)
                        job.last_output_at = time.time()
                        if job.manifest_path is None:
                            job.manifest_path = newest_run_manifest(cache_dir, job.started_at)
                        if len(job.logs) > 600:
                            del job.logs[:100]
            job.return_code = process.wait()
            if job.cancel_requested:
                self._finish_cancelled(job)
                return
            report_path = _resumed_run_report(cache_dir, options.resume_run_id, job.started_at)
            if report_path is None:
                report_path = newest_run_report(cache_dir, existing_reports, job.started_at)
            if report_path:
                job.report = json.loads(report_path.read_text(encoding="utf-8"))
                manifest_value = job.report.get("run_manifest_path")
                if manifest_value:
                    job.manifest_path = Path(str(manifest_value))
                final_state, final_message = classify_report(job.report, job.return_code)
                if final_state == "waiting_review":
                    try:
                        export_waiting_review_package(cache_dir, job.report, report_path)
                    except (ReviewPackageError, OSError, RuntimeError) as exc:
                        job.logs.append(f"人工复核表导出失败: {exc}")
                job.artifacts = register_report_artifacts(self.artifacts, job.report, report_path)
                job.state, job.message = final_state, final_message
            elif job.return_code == 0:
                job.state, job.message = "complete", "生成已完成"
            else:
                job.state = "failed"
                job.message = _failure_message(job.logs)
        except Exception as exc:
            job.logs.append(f"本地界面调用失败: {exc}")
            job.state = "failed"
            job.message = "本地界面无法完成本次运行"
        finally:
            job.process = None
            job.finished_at = time.time()
            if options.revision_constraints:
                final_feedback_status = (
                    "resolved" if job.state == "complete"
                    else "received" if job.state == "cancelled"
                    else "waiting_user"
                )
                _record_job_feedback_status(
                    job,
                    options.revision_constraints,
                    final_feedback_status,
                    child_job_id=job.job_id,
                    child_run_id=_run_id_from_report(job.report or _live_manifest_report(job)),
                    resolution_note=job.message,
                )
            with self._lock:
                if self._active_job_id == job.job_id:
                    self._active_job_id = None

    @staticmethod
    def _finish_cancelled(job: Job) -> None:
        job.state = "cancelled"
        job.message = "本次运行已停止"


def _record_job_feedback_status(job: Job, constraints_path: str, status: str, **kwargs) -> None:
    try:
        record_feedback_status(constraints_path, status, **kwargs)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        job.logs.append(f"反馈状态记录失败: {exc}")


def _failure_message(logs: list[str]) -> str:
    markers = (
        "ValueError:",
        "VisualCandidateError:",
        "Brief 读取失败:",
        "Brief读取失败:",
        "自动管线运行失败:",
        "自动管线发布失败:",
    )
    for line in reversed(logs):
        text = str(line).strip()
        for marker in markers:
            if marker in text:
                detail = text.split(marker, 1)[1].strip()
                if detail:
                    return f"运行失败：{detail}"
    return "运行失败，请展开日志查看详细原因"


def serialize_brief(brief: dict) -> str:
    keys = [
        "项目名称",
        "项目区域文件名",
        "脚本类型",
        "品牌或客户",
        "行业或活动类别",
        "视频时长",
        "风格调性",
        "必须出现的内容",
        "禁忌或不能出现的内容",
        "素材目录或素材文件",
        "备注",
    ]
    lines = [f"{key}：{brief.get(key, '')}" for key in keys]
    if brief.get("planning_events"):
        lines.append(
            "M+规划事件JSON："
            + json.dumps(
                brief["planning_events"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    if brief.get("creative_strategy_selection"):
        lines.append(
            "M+创作策略绑定JSON："
            + json.dumps(
                brief["creative_strategy_selection"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return "\n".join(lines) + "\n"


def preflight_payload(payload: LaunchPayload) -> PreflightReport:
    if payload.options.review_workbook:
        return review_import_preflight(payload)
    writer_provider, _, editor_provider, _, _ = resolve_model_route(payload.options)
    return run_launch_preflight(
        brief_data=payload.brief.to_brief(),
        source_root=payload.options.source_root,
        project_dir=payload.brief.material_path,
        output_dir=payload.options.output_dir,
        asset_cache_root=DEFAULT_ASSET_CACHE_ROOT,
        allow_cached_analysis=payload.options.allow_cached_analysis,
        quality_profile=payload.options.quality_profile,
        writer_provider=writer_provider,
        editor_provider=editor_provider,
        asset_package=payload.options.asset_package or None,
    )


def review_import_preflight(payload: LaunchPayload) -> PreflightReport:
    checks: list[CheckResult] = []
    review = Path(payload.options.review_workbook).expanduser().resolve()
    run_id = payload.options.resume_run_id.strip()
    if not review.is_file() or review.suffix.casefold() != ".xlsx":
        checks.append(CheckResult("人工复核表", "fail", f"请选择已填写的 .xlsx 复核表: {review}"))
    elif not run_id:
        checks.append(CheckResult("来源运行", "fail", "人工复核表必须绑定来源 run"))
    else:
        try:
            run_dir = find_history_run_dir(run_id)
            cache_dir = run_dir.parent.parent
            validate_review_workbook(cache_dir, review, run_id=run_id)
            checks.extend((
                CheckResult("人工复核表", "pass", f"来源、机器审计和不可编辑内容校验通过: {review.name}"),
                CheckResult("来源运行", "pass", f"将恢复 run {run_id}，仅执行人工审核与发布门"),
            ))
        except (VisualCandidateError, ReviewPackageError, OSError, json.JSONDecodeError) as exc:
            checks.append(CheckResult("人工复核表", "fail", str(exc)))
    return PreflightReport(
        brief=payload.brief.to_brief(),
        project_dir=None,
        output_dir=Path(payload.options.output_dir).expanduser(),
        checks=tuple(checks),
    )


def resolve_model_route(options: LaunchOptions) -> tuple[str, str, str, str, str]:
    if options.model_profile == "adaptive":
        return (
            "deepseek-http",
            "deepseek-v4-pro",
            "minimax-cli",
            "MiniMax-M3",
            "",
        )
    if options.model_profile == "minimax-m3":
        return (
            "minimax-cli",
            "MiniMax-M3",
            "minimax-cli",
            "MiniMax-M3",
            "",
        )
    if options.model_profile == "deepseek-only":
        return (
            "deepseek-http",
            "deepseek-v4-pro",
            "deepseek-http",
            "deepseek-v4-pro",
            "",
        )
    if options.model_profile == "dual-ab":
        return (
            "deepseek-http",
            "deepseek-v4-pro",
            "minimax-cli",
            "MiniMax-M2.7-highspeed",
            "",
        )
    return (
        options.writer_provider,
        options.writer_model,
        options.editor_provider,
        options.editor_model,
        options.vision_model,
    )


def preflight_response(report: PreflightReport) -> dict:
    return {
        "passed": report.passed,
        "brief": report.brief,
        "project_dir": str(report.project_dir or ""),
        "output_dir": str(report.output_dir),
        "checks": [
            {"name": check.name, "status": check.status, "message": check.message}
            for check in report.checks
        ],
    }


def _historical_review_step_idempotency_key(
    proposal_id: str, step: str, issue_keys: tuple[str, ...] = (),
) -> str:
    digest = sha256_json({
        "proposal_id": proposal_id,
        "step": step,
        "issue_keys": list(issue_keys),
    })[:32]
    return f"history-review-{step}-{digest}"


def _block_historical_review_task(
    tasks, auth_token: str, *, task_id: str, proposal_digest: str,
    saga_step: str, error: Exception,
) -> None:
    latest = tasks.get_task(auth_token, task_id)
    if latest["status"] in {"completed", "failed", "cancelled", "blocked"}:
        return
    error_code = str(getattr(error, "code", "HISTORICAL_REVIEW_IMPORT_FAILED"))
    tasks.transition_task(
        auth_token,
        task_id=task_id,
        expected_task_version=latest["task_version"],
        status="blocked",
        error_code="HISTORICAL_REVIEW_IMPORT_BLOCKED",
        details={
            "proposal_digest": proposal_digest,
            "saga_step": saga_step,
            "resume_from": "authoritative_review_read_model",
            "error_code": error_code,
            "message": "历史复核导入未完成，已阻断；相同提案可从审核 read model 继续",
        },
    )


def _run_historical_review_import_saga(
    *, tasks, formal_review_service, auth_token: str, task: dict,
    proposal_id: str, proposal_digest: str, workspace_id: str,
    project_id: str, expected_project_version: int, report: dict,
) -> dict:
    saga_step = "task_resume"
    try:
        current_task = task
        if current_task["status"] == "proposed":
            current_task = tasks.transition_task(
                auth_token, task_id=proposal_id,
                expected_task_version=current_task["task_version"], status="waiting_confirmation",
                details={"proposal_digest": proposal_digest},
            )
        if current_task["status"] in {"proposed", "waiting_confirmation", "blocked"}:
            current_task = tasks.transition_task(
                auth_token, task_id=proposal_id,
                expected_task_version=current_task["task_version"], status="executing",
                details={
                    "proposal_digest": proposal_digest,
                    "resume_from": "authoritative_review_read_model",
                },
            )
        elif current_task["status"] != "executing":
            raise WorkspaceServiceError("HISTORICAL_REVIEW_TASK_TERMINAL", "复核导入任务状态不能继续执行")

        saga_step = "start_review"
        review = formal_review_service.start_review(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=expected_project_version,
            idempotency_key=f"history-review-start-{proposal_id}",
        )
        mapped_returns = map_historical_review_returns(
            list(report["projects"][0].get("issues") or []),
            list(review.get("issues") or []),
        )
        returned = mapped_returns["issue_keys"]
        returned_shots = mapped_returns["shot_nos"]
        returned_set = set(returned)
        pending_return = tuple(
            str(issue["issue_key"])
            for issue in review.get("issues") or []
            if str(issue.get("issue_key") or "") in returned_set
            and str(issue.get("status") or "open") != "return"
        )
        if pending_return:
            saga_step = "return_decisions"
            review = formal_review_service.decide(
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=review["project_version"],
                issue_keys=pending_return,
                decision="return",
                note="历史人工复核表包含需修改意见",
                idempotency_key=_historical_review_step_idempotency_key(
                    proposal_id, "return", pending_return,
                ),
            )
        pending_approve = tuple(
            str(issue["issue_key"])
            for issue in review.get("issues") or []
            if str(issue.get("issue_key") or "") not in returned_set
            and str(issue.get("status") or "") in {"open", "recheck"}
        )
        if pending_approve:
            saga_step = "approve_decisions"
            review = formal_review_service.decide(
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=review["project_version"],
                issue_keys=pending_approve,
                decision="approve",
                note="由已核验历史人工复核表导入",
                idempotency_key=_historical_review_step_idempotency_key(
                    proposal_id, "approve", pending_approve,
                ),
            )
        if report["projects"][0]["human_review_status"] == "quality_pass" and review.get("ready_for_final"):
            saga_step = "final_confirmation"
            review = formal_review_service.confirm_final(
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=review["project_version"],
                note="由已核验历史人工复核表完成整片确认",
                idempotency_key=_historical_review_step_idempotency_key(
                    proposal_id, "final",
                ),
            )
        response = {
            "type": "historical_review_imported",
            "status": "completed",
            "review_state": review["state"],
            "returned_shot_count": len(returned_shots),
            "production_certification": "BLOCK",
        }
        saga_step = "task_complete"
        tasks.transition_task(
            auth_token, task_id=proposal_id,
            expected_task_version=current_task["task_version"], status="completed",
            details={"response": response, "returned_shot_count": len(returned_shots)},
        )
        return response
    except Exception as exc:
        _block_historical_review_task(
            tasks,
            auth_token,
            task_id=proposal_id,
            proposal_digest=proposal_digest,
            saga_step=saga_step,
            error=exc,
        )
        if isinstance(exc, WorkspaceServiceError):
            raise
        raise WorkspaceServiceError(
            "HISTORICAL_REVIEW_IMPORT_FAILED",
            "历史复核导入未完成，任务已阻断；请使用相同提案重试",
        ) from exc


def classify_report(report: dict, return_code: int | None = None) -> tuple[str, str]:
    human_status = str(report.get("human_review_status") or "")
    quality_status = str(report.get("quality_review_status") or human_status)
    reviewer_kind = str(report.get("reviewer_kind") or "human")
    reviewer_label = "独立 AI 质量复核" if reviewer_kind == "independent_ai" else "人工复核"
    if (
        report.get("exit_code") == 0
        and report.get("release_decision") == "release_pass"
        and quality_status == "quality_pass"
    ):
        return "complete", f"{reviewer_label}已通过"
    if report.get("error") or report.get("exit_code") == 1:
        return "failed", "运行失败，请查看日志"
    if report.get("machine_audit_status") == "fail":
        return "machine_blocked", "已生成，机器审核未通过"
    if quality_status == "quality_pass":
        return "complete", f"{reviewer_label}已通过"
    if quality_status == "quality_fail":
        return "review_invalid", f"{reviewer_label}未通过"
    if human_status == "invalid":
        return "review_invalid", "已生成，人工复核状态无效"
    if human_status in {"pending", "pending_review", ""}:
        if report.get("media_readiness_status") == "not_ready":
            return "waiting_review", "脚本文档机器审核通过，成片素材待补，等待人工复核"
        return "waiting_review", "机器审核通过，等待人工复核"
    if human_status == "quality_pass" or report.get("release_decision") in {"pass", "trial_pass"}:
        return "complete", "质量门已通过"
    if return_code not in {None, 0, 2, 3}:
        return "failed", "运行失败，请查看日志"
    return "waiting_review", "已生成，等待人工复核"


def read_stage_progress(report: dict | None) -> tuple[list[dict], dict | None]:
    if not report:
        return [], None
    manifest_path = Path(str(report.get("run_manifest_path") or ""))
    if not manifest_path.is_file():
        return [], None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], None
    records = manifest.get("stages")
    if not isinstance(records, dict):
        return [], None
    stages = []
    for stage_id in STAGE_ORDER:
        record = records.get(stage_id.value) or {}
        stages.append({
            "stage_id": stage_id.value,
            "status": str(record.get("status") or "not_started"),
            "attempt": int(record.get("attempt") or 0),
            "error": record.get("error"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "elapsed_seconds": _stage_elapsed_seconds(record),
            "reused_from_run": str(record.get("reused_from_run") or ""),
        })
    active = next(
        (
            stage for stage in stages
            if stage["status"] in {"failed", "blocked", "waiting_human_review", "running"}
        ),
        None,
    )
    if active is None:
        active = next(
            (
                stage for stage in stages
                if stage["status"] not in {"succeeded", "succeeded_with_warnings"}
            ),
            None,
        )
    return stages, active


def stage_progress_summary(stages: list[dict]) -> dict:
    total = len(stages)
    completed = sum(
        stage.get("status") in {"succeeded", "succeeded_with_warnings"}
        for stage in stages
    )
    return {
        "completed": completed,
        "total": total,
        "percent": round(completed * 100 / total) if total else 0,
    }


def model_status_summary(options: LaunchOptions, stages: list[dict], active_stage: dict | None) -> list[dict]:
    writer_provider, writer_model, editor_provider, editor_model, vision_model = resolve_model_route(options)
    status_by_stage = {stage["stage_id"]: stage["status"] for stage in stages}
    active_id = str((active_stage or {}).get("stage_id") or "")

    def state(stage_id: str) -> str:
        value = status_by_stage.get(stage_id, "not_started")
        if stage_id == active_id and value == "running":
            return "running"
        if value in {"succeeded", "succeeded_with_warnings"}:
            return "complete"
        if value in {"failed", "blocked"}:
            return "blocked"
        return "waiting"

    rows = [
        {"role": "视觉理解", "provider": "MiniMax", "model": vision_model or "provider-managed", "stage": "visual_analysis", "state": state("visual_analysis")},
    ]
    if options.quality_profile == "draft":
        rows.append({
            "role": "文本草稿",
            "provider": "本地规则",
            "model": "未调用外部文本模型",
            "stage": "script_draft",
            "state": state("script_draft"),
        })
    else:
        rows.extend([
            {"role": "逻辑初稿", "provider": writer_provider, "model": writer_model, "stage": "script_draft", "state": state("script_draft")},
            {"role": "中文编辑", "provider": editor_provider, "model": editor_model, "stage": "script_draft", "state": state("script_draft")},
        ])
    return rows


def _stage_elapsed_seconds(record: dict) -> int | None:
    started = _parse_utc(record.get("started_at"))
    if started is None:
        return None
    finished = _parse_utc(record.get("finished_at")) or datetime.now(timezone.utc)
    return max(0, round((finished - started).total_seconds()))


def _parse_utc(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def newest_run_manifest(cache_dir: Path, started_at: float | None) -> Path | None:
    candidates = [path for path in cache_dir.glob("runs/*/run_manifest.json") if path.is_file()]
    if started_at is not None:
        candidates = [path for path in candidates if path.stat().st_mtime >= started_at - 2]
    return max(candidates, key=lambda item: item.stat().st_mtime, default=None)


def _live_manifest_report(job: Job) -> dict | None:
    if job.manifest_path is None and job.cache_dir is not None:
        job.manifest_path = newest_run_manifest(job.cache_dir, job.started_at)
    if job.manifest_path and job.manifest_path.is_file():
        return {"run_manifest_path": str(job.manifest_path)}
    return None


def _run_id_from_report(report: dict | None) -> str:
    if not report:
        return ""
    if report.get("run_id"):
        return str(report["run_id"])
    manifest_path = Path(str(report.get("run_manifest_path") or ""))
    if manifest_path.is_file():
        try:
            return str(json.loads(manifest_path.read_text(encoding="utf-8")).get("run_id") or "")
        except (OSError, json.JSONDecodeError):
            return ""
    return ""


def newest_run_report(cache_dir: Path, existing: set[Path], started_at: float | None) -> Path | None:
    candidates = []
    for path in cache_dir.glob("runs/*/run_report.json"):
        resolved = path.resolve()
        if resolved in existing or not path.is_file():
            continue
        if started_at and path.stat().st_mtime + 2 < started_at:
            continue
        candidates.append(path)
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _resumed_run_report(cache_dir: Path, run_id: str, started_at: float | None) -> Path | None:
    if not run_id:
        return None
    candidate = cache_dir / "runs" / run_id / "run_report.json"
    if not candidate.is_file():
        return None
    if started_at and candidate.stat().st_mtime + 2 < started_at:
        return None
    return candidate


def register_report_artifacts(registry: ArtifactRegistry, report: dict, report_path: Path) -> list[dict]:
    review_package = _existing_review_package_path(report, report_path)
    quality_review_label = (
        "查看独立 AI 复核证据"
        if report.get("reviewer_kind") == "independent_ai"
        else "查看质量复核报告"
    )
    items = [
        registry.register(report.get("output_path"), "workbook", "下载脚本 Excel"),
        registry.register(review_package, "review_workbook", "下载人工复核表"),
        registry.register(
            portable_visual_review_path(report, report_path),
            "review",
            "下载跨设备图文预览",
        ),
        registry.register(report.get("audit_path"), "audit", "查看机器审计"),
        registry.register(
            report.get("quality_review_report_path"),
            "quality_review",
            quality_review_label,
        ),
        registry.register(report_path, "report", "运行报告"),
    ]
    return [item for item in items if item]


def portable_visual_review_path(report: dict, report_path: Path) -> Path | str | None:
    review_path = Path(str(report.get("visual_review_html") or "")).expanduser()
    workbook_path = Path(str(report.get("output_path") or "")).expanduser()
    if not workbook_path.is_file():
        return review_path if review_path.is_file() else None
    if (
        report.get("release_decision") != "release_pass"
        or (review_path.is_file() and review_path.stat().st_mtime >= workbook_path.stat().st_mtime)
    ):
        return review_path if review_path.is_file() else None
    digest = sha256_file(workbook_path)
    target = (
        report_path.resolve().parent.parent.parent
        / "portable_previews"
        / f"{workbook_path.stem}-{digest[:16]}.html"
    )
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        write_visual_review_html_from_workbook(workbook_path, target)
    return target


def export_waiting_review_package(
    cache_dir: str | Path,
    report: dict,
    report_path: str | Path,
) -> Path:
    run_id = str(report.get("run_id") or "").strip()
    if not run_id:
        raise ReviewPackageError("运行报告缺少 run_id，无法导出人工复核表")
    existing = _existing_review_package_path(report, Path(report_path))
    if existing.is_file():
        return existing
    target = _canonical_review_package_path(report, Path(report_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    return export_review_workbook(cache_dir, target, run_id=run_id)


def _register_candidate_previews(
    registry: ArtifactRegistry,
    result: dict,
    shot_no: str,
) -> None:
    current = dict(result.get("current") or {})
    current_artifact = registry.register(
        current.get("path"), "candidate_image", f"镜{shot_no}当前图"
    )
    current["preview_id"] = current_artifact["id"] if current_artifact else ""
    result["current"] = current
    for candidate in result.get("candidates") or []:
        artifact = registry.register(
            candidate.get("预览路径") or candidate.get("原图路径"),
            "candidate_image",
            f"镜{shot_no}候选图",
        )
        candidate["preview_id"] = artifact["id"] if artifact else ""


def _canonical_review_package_path(report: dict, report_path: Path) -> Path:
    output = Path(str(report.get("output_path") or "脚本.xlsx"))
    run_id = str(report.get("run_id") or report_path.parent.name).strip()
    project_cache = report_path.parent.parent.parent
    return project_cache / "reviews" / f"{run_id}-{output.stem}-人工复核.xlsx"


def _existing_review_package_path(report: dict, report_path: Path) -> Path:
    explicit = Path(str(report.get("review_workbook_path") or "")).expanduser()
    if str(explicit) not in {"", "."} and explicit.is_file():
        return explicit.resolve()

    canonical = _canonical_review_package_path(report, report_path)
    if canonical.is_file():
        return canonical

    run_id = str(report.get("run_id") or report_path.parent.name).strip()
    candidates = [
        path for path in canonical.parent.glob(f"{run_id}-*-人工复核.xlsx")
        if path.is_file() and not path.name.startswith(".~")
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=canonical)


class LocalJobExecutionBackend:
    """Production adapter from controlled E03 actions to the existing JobManager."""

    def __init__(
        self,
        jobs: JobManager,
        artifacts: ArtifactRegistry,
        resolver: RunArtifactResolver,
        brief_store: BriefArtifactStore,
        runtime_config: dict,
    ) -> None:
        self.jobs = jobs
        self.artifacts = artifacts
        self.resolver = resolver
        self.brief_store = brief_store
        self.runtime_config = runtime_config

    def inspect_action(
        self,
        action: str,
        parameters: dict,
        brief_snapshot: dict | None,
    ) -> dict:
        if action == "prepare_human_review":
            readiness = self.review_readiness(parameters["run_id"])
            return {
                "passed": bool(readiness["ready"]),
                "calls_models": False,
                "checks": readiness.get("checks", []),
                "error_code": None if readiness["ready"] else "REVIEW_REQUIRED",
                "message": readiness["summary"],
            }
        payload = self._payload_for(action, parameters, brief_snapshot)
        report = preflight_payload(payload)
        if action == "start_generation":
            calls_models = True
        elif action == "retry_stage":
            calls_models = parameters.get("stage_id") in {
                "visual_analysis", "script_plan", "script_draft"
            }
        else:
            calls_models = (
                dict(parameters.get("modification_plan") or {}).get("category")
                != "image"
            )
        return {
            "passed": report.passed,
            "calls_models": calls_models,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "message": check.message,
                }
                for check in report.checks
            ],
            "error_code": None if report.passed else "INVALID_INPUT",
            "message": "真实运行预检通过" if report.passed else "真实运行预检未通过",
        }

    def start_action(
        self,
        action: str,
        parameters: dict,
        brief_snapshot: dict | None,
    ) -> dict:
        try:
            if action == "fork_revision":
                context = _delivery_context_payload(parameters.get("delivery_context"))
                self._verify_delivery_context(context)
                payload = self._payload_for("fork_revision", parameters, brief_snapshot)
                plan = dict(parameters["modification_plan"])
                feedback = FeedbackPayload(
                    category=plan["category"],
                    scope=plan.get("scope", "whole_film"),
                    shot_no=plan.get("shot_no", ""),
                    comment=plan["comment"],
                    desired_change=plan.get("desired_change", ""),
                    keep_fields=list(plan.get("keep_fields") or []),
                )
                result = self._start_delivery_revision(
                    context=context, feedback=feedback, payload=payload
                )
                return dict(result["child_job"])
            payload = self._payload_for(action, parameters, brief_snapshot)
            return self.jobs.start(payload).snapshot()
        except HTTPException as exc:
            detail = exc.detail
            message = detail if isinstance(detail, str) else str((detail or {}).get("message") or "真实执行条件未通过")
            raise WorkspaceServiceError("INVALID_INPUT", message) from exc
        except (ArtifactResolutionError, ReviewPackageError, OSError, ValueError) as exc:
            raise WorkspaceServiceError(
                str(getattr(exc, "code", "INVALID_INPUT")), str(exc)
            ) from exc

    def get_job(self, job_id: str) -> dict:
        return self.jobs.get(job_id).snapshot()

    def cancel_job(self, job_id: str) -> dict:
        return self.jobs.cancel(job_id).snapshot()

    def review_readiness(self, run_id: str) -> dict:
        try:
            report_ref = self.resolver.resolve_artifact(run_id, "run_report")
            report = json.loads(report_ref.path.read_text(encoding="utf-8"))
            manifest = self.resolver.read_manifest(run_id)
        except (ArtifactResolutionError, OSError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError(
                str(getattr(exc, "code", "ARTIFACT_INVALID")),
                "无法从经 SHA 校验的 run 证据判断人工复核资格",
            ) from exc
        machine = str(report.get("machine_audit_status") or "")
        human = str(report.get("human_review_status") or "")
        human_stage = dict((manifest.get("stages") or {}).get("human_review") or {})
        ready = machine == "pass" and human in {"", "pending", "pending_review"}
        if human in {"quality_pass", "quality_fail"}:
            summary = "该 run 已有人工质量结论，无需重新把 pending 冒充为待审核。"
        elif machine != "pass":
            summary = "机器审核尚未通过，不能准备人工复核包。"
        elif ready:
            summary = "机器审核通过，当前仍明确等待人工审核。"
        else:
            summary = "当前 run 的审核状态不满足复核包准备条件。"
        return {
            "type": "review_readiness",
            "title": "人工审核资格",
            "summary": summary,
            "run_id": run_id,
            "ready": ready,
            "machine_audit_status": machine or "unknown",
            "human_review_status": human or "pending",
            "human_stage_status": str(human_stage.get("status") or "unknown"),
            "checks": [
                {"name": "机器审核", "status": "pass" if machine == "pass" else "fail"},
                {"name": "人工结论", "status": "pass" if ready else "blocked"},
            ],
            "next_steps": [
                "显式确认后准备人工复核包。"
                if ready
                else "先处理机器阻断或核对已有人工结论。"
            ],
        }

    def prepare_human_review(self, run_id: str) -> dict:
        readiness = self.review_readiness(run_id)
        if not readiness["ready"]:
            raise WorkspaceServiceError("REVIEW_REQUIRED", readiness["summary"])
        report_ref = self.resolver.resolve_artifact(run_id, "run_report")
        report = json.loads(report_ref.path.read_text(encoding="utf-8"))
        run_dir = report_ref.path.parent
        package = export_waiting_review_package(
            run_dir.parent.parent,
            report,
            report_ref.path,
        )
        digest = sha256_file(package)
        download = self.artifacts.register(package, "review_workbook", "下载人工复核包")
        return {
            "artifact_ref": f"artifact://review/{run_id}/{digest[:20]}",
            "sha256": digest,
            "download_artifact": download,
        }

    def finalized_artifacts(self, snapshot: dict) -> dict | None:
        run_id = str(snapshot.get("run_id") or "").strip()
        if not run_id:
            return None
        try:
            manifest_ref = self.resolver.resolve_manifest(run_id)
            manifest = self.resolver.read_manifest(
                run_id, expected_sha256=manifest_ref.sha256
            )
        except ArtifactResolutionError as exc:
            raise WorkspaceServiceError(exc.code, exc.message) from exc
        result = {
            "run_id": run_id,
            "parent_run_id": str(manifest.get("parent_run_id") or ""),
            "manifest_ref": manifest_ref.ref,
            "manifest_sha256": manifest_ref.sha256,
            "artifact_ref": None,
            "content_sha256": None,
        }
        try:
            workbook = self.resolver.resolve_artifact(run_id, "workbook")
        except ArtifactResolutionError as exc:
            if exc.code != "NOT_FOUND":
                raise WorkspaceServiceError(exc.code, exc.message) from exc
        else:
            result["artifact_ref"] = workbook.ref
            result["content_sha256"] = workbook.sha256
        return result

    def write_revision_report(
        self,
        *,
        parent_context: dict,
        child_context: VersionDeliveryContext,
        modification_plan: dict,
        machine_review: dict,
        human_review: dict,
    ) -> dict:
        parent = _delivery_context_payload(parent_context)
        child = child_context.as_dict()
        self._verify_delivery_context(parent)
        self._verify_delivery_context(child)
        parent_shots = self._context_shots(parent)
        child_shots = self._context_shots(child)
        plan = dict(modification_plan or {})
        target_field = str(plan.get("target_field") or "").strip()
        target_shot = str(plan.get("shot_no") or "").strip()
        if not target_field or not target_shot:
            raise WorkspaceServiceError(
                "INVALID_INPUT", "revision report 需要已解析的镜头与权威字段"
            )
        diff = compare_versions(
            project_id=str(child["project_id"]),
            parent_version_id=str(parent["version_id"]),
            child_version_id=str(child["version_id"]),
            parent_shots=parent_shots,
            child_shots=child_shots,
        )
        requested_value = str(plan.get("requested_value") or "").strip()
        unresolved = []
        if requested_value and not _requested_value_applied(
            diff, shot_no=target_shot, field=target_field, value=requested_value
        ):
            unresolved.append({
                "code": "REQUESTED_VALUE_MISSING",
                "shot_no": target_shot,
                "field": target_field,
                "requested_value": requested_value,
            })
        report = build_revision_report(
            diff=diff,
            original_feedback=str(plan.get("comment") or ""),
            structured_intent={
                "category": str(plan.get("category") or ""),
                "scope": str(plan.get("scope") or ""),
                "shot_no": target_shot,
                "field": target_field,
                "requested_value": requested_value,
            },
            expected_impact={"fields": [target_field], "shot_nos": [target_shot]},
            locked_fields=_locked_fields(parent_shots, target_shot, target_field),
            rerun_nodes=["script_draft", "visual_binding", "workbook_render", "machine_audit"],
            unresolved_items=unresolved,
            machine_review={"status": str(machine_review.get("machine_audit_status") or "pending")},
            human_review={"status": str(human_review.get("status") or "pending")},
            publication={"status": "not_published"},
        )
        payload = {
            "schema_version": "mplus-revision-delivery-evidence/v1",
            "parent_context": parent,
            "child_context": child,
            "n_chain": {
                "parent_shot_count": len(parent_shots),
                "child_shot_count": len(child_shots),
                "parent_adjacent_review_count": max(0, len(parent_shots) - 1),
                "child_adjacent_review_count": max(0, len(child_shots) - 1),
            },
            "revision_report": report,
        }
        directory = Path(self.runtime_config["data_root"]) / "revision_reports"
        path = directory / f"{child['version_id']}.json"
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise WorkspaceServiceError(
                    "ARTIFACT_INVALID", "immutable revision report 无法读取"
                ) from exc
            if existing != payload:
                raise WorkspaceServiceError(
                    "INTEGRITY_ERROR", "immutable revision report 已存在但内容不一致"
                )
        else:
            write_json_atomic(path, payload)
        digest = sha256_file(path)
        self.artifacts.register(path, "revision_report", "修订差异报告")
        return {
            "artifact_ref": f"artifact://revision/{child['version_id']}/{digest}",
            "sha256": digest,
            "version_id": child["version_id"],
        }

    def _payload_for(
        self,
        action: str,
        parameters: dict,
        brief_snapshot: dict | None,
    ) -> LaunchPayload:
        if action == "start_generation":
            if not brief_snapshot:
                raise WorkspaceServiceError(
                    "BRIEF_ARTIFACT_UNAVAILABLE", "项目没有可解析的冻结 Brief artifact"
                )
            brief = self.brief_store.read(
                brief_snapshot["brief_ref"], brief_snapshot["brief_sha256"]
            )
            script_type = normalize_script_type(brief.get("脚本类型") or "")
            runtime = self.runtime_config
            options = LaunchOptions(
                source_root=str(runtime.get("source_root") or DEFAULT_SOURCE_ROOT),
                asset_package=str(
                    dict(runtime.get("asset_packages") or {}).get(script_type)
                    or (runtime.get("asset_package") if script_type == "发布会" else "")
                    or ""
                ),
                output_dir=str(runtime.get("exports_root") or DEFAULT_OUTPUT_ROOT),
                quality_profile="client-review",
                model_profile=str(runtime.get("model_profile") or "adaptive"),
            )
            return LaunchPayload(brief=brief_form_from_brief(brief), options=options)
        if action == "fork_revision":
            _delivery_context_payload(parameters.get("delivery_context"))
            return self._payload_for("start_generation", parameters, brief_snapshot)
        run_id = str(parameters.get("run_id") or parameters.get("parent_run_id") or "")
        manifest = self.resolver.resolve_manifest(run_id)
        payload = launch_payload_from_run(manifest.path.parent)
        if action == "retry_stage":
            options = payload.options.model_copy(
                update={
                    "resume_run_id": "",
                    "retry_stage": "",
                    "parent_run_id": run_id,
                    "from_stage": parameters["stage_id"],
                    "allow_cached_analysis": True,
                }
            )
            return payload.model_copy(update={"options": options})
        return payload

    def _start_delivery_revision(
        self,
        *,
        context: dict,
        feedback: FeedbackPayload,
        payload: LaunchPayload,
    ) -> dict:
        parent_run_id = str(context["run_id"])
        root = Path(self.runtime_config["data_root"]) / "revision_feedback" / parent_run_id
        stored = store_user_feedback(
            root,
            run_id=parent_run_id,
            **feedback.model_dump(),
        )
        options = payload.options.model_copy(update={
            "resume_run_id": "",
            "from_stage": stored["restart_stage"],
            "retry_stage": "",
            "revision_constraints": stored["constraints_path"],
            "parent_run_id": parent_run_id,
            "allow_cached_analysis": True,
        })
        child = self.jobs.start(payload.model_copy(update={"options": options}))
        return {"feedback": stored, "child_job": child.snapshot()}

    def _verify_delivery_context(self, context: dict) -> None:
        if context.get("schema_version") != "mplus-version-delivery-context/v1":
            raise WorkspaceServiceError("INTEGRITY_ERROR", "VersionDeliveryContext schema 不受支持")
        run_id = str(context.get("run_id") or "")
        manifest = dict(context.get("manifest") or {})
        resolved = self.resolver.resolve_manifest(
            run_id, expected_sha256=str(manifest.get("sha256") or "")
        )
        if resolved.ref != manifest.get("ref"):
            raise WorkspaceServiceError("INTEGRITY_ERROR", "delivery manifest ref 不一致")
        for stage in ("script_plan", "script_draft", "visual_binding"):
            expected = dict((context.get("artifacts") or {}).get(stage) or {})
            artifact = self.resolver.resolve_stage_artifact(
                run_id, stage, expected_manifest_sha256=resolved.sha256
            )
            if artifact.ref != expected.get("ref") or artifact.sha256 != expected.get("sha256"):
                raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", f"delivery {stage} 已漂移")

    def _context_shots(self, context: dict) -> list[dict]:
        expected = dict(context["artifacts"]["visual_binding"])
        artifact = self.resolver.resolve_stage_artifact(
            str(context["run_id"]),
            "visual_binding",
            expected_manifest_sha256=str(context["manifest"]["sha256"]),
        )
        if artifact.ref != expected["ref"] or artifact.sha256 != expected["sha256"]:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "visual_binding 与 delivery context 不一致")
        try:
            payload = json.loads(artifact.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "visual_binding 无法读取") from exc
        return version_shots_from_artifacts(
            list(payload.get("script_rows") or []),
            list(payload.get("detail_rows") or []),
            bindings=list(payload.get("bindings") or []),
        )


def _delivery_context_payload(value: object) -> dict:
    if not isinstance(value, dict):
        raise WorkspaceServiceError("INTEGRITY_ERROR", "修订缺少 VersionDeliveryContext")
    context = json.loads(json.dumps(value, ensure_ascii=False))
    expected = {
        "schema_version", "workspace_id", "project_id", "project_lock_version",
        "version_id", "run_id", "parent_version_id", "version_artifact",
        "manifest", "artifacts",
    }
    if set(context) != expected:
        raise WorkspaceServiceError("INTEGRITY_ERROR", "VersionDeliveryContext 字段不符合 C0 合同")
    return context


def _locked_fields(parent_shots: list[dict], target_shot: str, target_field: str) -> list[dict]:
    locks = []
    for shot in parent_shots:
        shot_no = str(shot.get("shot_no") or "")
        for field in sorted(dict(shot.get("fields") or {})):
            if shot_no == target_shot and field == target_field:
                continue
            locks.append({"shot_no": shot_no, "field": field})
        locks.append({"shot_no": shot_no, "field": "source_refs"})
    return locks


def _requested_value_applied(
    diff: dict, *, shot_no: str, field: str, value: str
) -> bool:
    for shot in diff.get("shot_diffs") or []:
        if str(shot.get("child_shot_no") or "") != shot_no:
            continue
        for changed in shot.get("field_diffs") or []:
            if changed.get("field") == field and changed.get("after") == value:
                return True
    return False


def brief_to_form(brief: dict) -> dict:
    duration_text = str(brief.get("视频时长") or "").strip().lower().replace("秒", "").removesuffix("s")
    try:
        duration_seconds = int(duration_text)
    except ValueError:
        duration_seconds = 60
    result = {
        "project_name": brief.get("项目名称", ""),
        "output_filename": brief.get("项目区域文件名", ""),
        "script_type": normalize_script_type(brief.get("脚本类型") or "发布会"),
        "duration_seconds": duration_seconds,
        "client": brief.get("品牌或客户", ""),
        "industry": brief.get("行业或活动类别", ""),
        "tone": brief.get("风格调性", ""),
        "required_content": brief.get("必须出现的内容", ""),
        "forbidden_content": brief.get("禁忌或不能出现的内容", ""),
        "material_path": brief.get("素材目录或素材文件", ""),
        "note": brief.get("备注", ""),
    }
    if brief.get("planning_events"):
        result["planning_events"] = brief["planning_events"]
    if brief.get("creative_strategy_selection"):
        result["creative_strategy_selection"] = brief[
            "creative_strategy_selection"
        ]
    return result


def brief_form_from_brief(brief: dict) -> BriefForm:
    values = brief_to_form(brief)
    form_type = (
        ControlledBriefForm
        if values.get("planning_events") or values.get("creative_strategy_selection")
        else BriefForm
    )
    return form_type(**values)


def system_checks() -> list[dict]:
    evolution = summarize_evolution_ledgers(
        DEFAULT_ASSET_CACHE_ROOT.glob("*/evolution_ledger.jsonl")
    )
    runtime = load_runtime_config()
    configured_packages = dict(runtime.get("asset_packages") or {})

    def script_library(script_type: str) -> Path:
        package = Path(str(configured_packages.get(script_type) or "")).expanduser()
        package_source = package / "sources"
        if (package / "asset_package.json").is_file():
            resolved = resolve_script_type_workbook(package_source, script_type)
            if resolved.is_file():
                return resolved
        return resolve_script_type_workbook(DEFAULT_SOURCE_ROOT, script_type)

    checks = [
        ("本地 Python", True, sys.version.split()[0]),
        *[
            (
                f"{metadata.display_name}资料库",
                (library := script_library(metadata.display_name)).is_file(),
                str(library),
            )
            for metadata in SCRIPT_TYPE_METADATA
        ],
        ("文案生成", None, "已配置，尚未验证" if os.environ.get("DEEPSEEK_API_KEY") else "尚未确认文案服务配置；请由管理员查看系统诊断"),
        ("视觉 AI", None, "已配置，尚未验证" if minimax_credentials_available("", "mmx") else "尚未配置视觉服务"),
        ("输出根目录", DEFAULT_OUTPUT_ROOT.parent.is_dir(), str(DEFAULT_OUTPUT_ROOT)),
        (
            "数据沉淀",
            True,
            f"AI 版本 {evolution['ai_output_count']} · 反馈 {evolution['event_counts'].get('feedback_received', 0)} · 最终采用 {evolution['adopted_version_count']} · 待审规则候选 {evolution['pending_rule_candidate_count']}",
        ),
    ]
    return [
        {"name": name, "status": "unverified" if ok is None else "pass" if ok else "fail", "message": message}
        for name, ok, message in checks
    ]


def history_roots() -> list[Path]:
    configured = Path(load_runtime_config()["runs_root"]).expanduser().resolve()
    roots = [configured]
    default = Path(DEFAULT_ASSET_CACHE_ROOT).expanduser().resolve()
    if configured == default:
        roots.extend(legacy_roots("runs"))
    return list(dict.fromkeys(path.expanduser().resolve() for path in roots))


def find_history_run_dir(run_id: str) -> Path:
    matches: dict[Path, Path] = {}
    for root in history_roots():
        try:
            match = find_run_dir(root, run_id)
            matches[Path(os.path.realpath(match))] = match
        except VisualCandidateError:
            pass
    if len(matches) != 1:
        raise VisualCandidateError("找不到唯一的运行版本")
    return next(iter(matches.values()))


def _remap_report_paths(report: dict) -> dict:
    result = dict(report)
    for key in (
        "output_path",
        "audit_path",
        "visual_review_html",
        "quality_review_report_path",
        "review_workbook_path",
        "run_manifest_path",
        "run_report_path",
    ):
        value = str(result.get(key) or "").strip()
        if value:
            result[key] = str(remap_legacy_path(value))
    return result


def scan_history(
    registry: ArtifactRegistry,
    limit: int | None = 300,
    *,
    legacy_runs: list[dict] | None = None,
    run_id: str | None = None,
) -> list[dict]:
    paths = []
    for root in history_roots():
        paths.extend(root.glob("*/runs/*/run_report.json"))
        paths.extend(root.glob("*/run_report.json"))
    unique = {path.resolve(): path.resolve() for path in paths if path.is_file()}
    rows = []
    for path in sorted(unique.values(), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            report = _remap_report_paths(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        state, message = classify_report(report, report.get("exit_code"))
        stages, active_stage = read_stage_progress(report)
        manifest_path = Path(str(report.get("run_manifest_path") or path.parent / "run_manifest.json"))
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        row_run_id = str(manifest.get("run_id") or report.get("run_id") or path.parent.name)
        if run_id is not None and row_run_id != run_id:
            continue
        brief = {}
        try:
            freeze = json.loads((path.parent / "input_freeze.json").read_text(encoding="utf-8"))
            brief = brief_to_form(dict(freeze.get("brief") or {}))
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        artifacts = register_report_artifacts(registry, report, path)
        rows.append({
            "run_id": str(manifest.get("run_id") or report.get("run_id") or path.parent.name),
            "parent_run_id": str(manifest.get("parent_run_id") or ""),
            "project_id": str(manifest.get("project_id") or report.get("project_id") or ""),
            "state": state,
            "message": message,
            "updated_at": path.stat().st_mtime,
            "report": report,
            "artifacts": artifacts,
            "stages": stages,
            "active_stage": active_stage,
            "brief": brief,
            "artifact_availability": "delivery_available" if artifacts else "metadata_only",
            "read_only_legacy": False,
        })
    catalog_path = Path(load_runtime_config()["legacy_catalog_path"])
    if catalog_path.is_file():
        try:
            rows.extend(_catalog_history_rows(registry, catalog_path, run_id=run_id))
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            pass
    if legacy_runs:
        rows.extend(_legacy_history_rows(registry, legacy_runs, remote=True, run_id=run_id))
    by_run_id = {}
    for row in rows:
        run_id = str(row.get("run_id") or "")
        current = by_run_id.get(run_id)
        if current is None or (current.get("read_only_legacy") and not row.get("read_only_legacy")):
            by_run_id[run_id] = row
    ordered = sorted(by_run_id.values(), key=lambda item: (
        float(item.get("updated_at") or 0), str(item.get("run_id") or "")), reverse=True)
    return ordered if limit is None else ordered[:max(1, min(int(limit), 1000))]


def _catalog_history_rows(registry: ArtifactRegistry, catalog_path: Path, *, run_id: str | None = None) -> list[dict]:
    from .legacy_archive import LEGACY_RECEIPT_SCHEMA_VERSION, read_legacy_catalog

    catalog = read_legacy_catalog(catalog_path, verify_files=False)
    is_receipt = catalog.get("schema_version") == LEGACY_RECEIPT_SCHEMA_VERSION
    archive_root = Path(str(catalog.get("archive_root") or ""))
    return _legacy_history_rows(
        registry,
        list(catalog.get("runs") or []),
        remote=is_receipt,
        archive_root=archive_root, run_id=run_id,
    )


def _legacy_history_rows(
    registry: ArtifactRegistry,
    runs: list[dict],
    *,
    remote: bool,
    archive_root: Path | None = None,
    run_id: str | None = None,
) -> list[dict]:
    from .script_type_adapter import metadata_for_category_id

    rows = []
    for run in runs:
        if run_id is not None and str(run.get("run_id") or "") != run_id:
            continue
        availability = str(run.get("artifact_availability") or "metadata_only")
        artifacts = []
        for index, record in enumerate(run.get("delivery_files") or []):
            filename = Path(str(record.get("path") or "")).name
            kind = "review_workbook" if any(token in filename for token in ("客户", "终审", "复核")) else "workbook"
            label = "下载历史终审稿" if kind == "review_workbook" else "下载历史交付稿"
            registered = None
            if remote and record.get("storage_ref"):
                registered = registry.register_remote(
                    record["storage_ref"],
                    record["sha256"],
                    kind=kind,
                    label=label,
                    filename=filename,
                    size_bytes=record["size_bytes"],
                )
            elif archive_root is not None and archive_root.is_dir():
                registered = registry.register(
                    archive_root / str(record.get("path") or ""),
                    kind,
                    label,
                )
            if registered:
                artifacts.append(registered)
        category_id = str(run.get("category_id") or "")
        try:
            script_type = metadata_for_category_id(category_id).display_name
        except ValueError:
            script_type = "历史类型未记录"
        status = str(run.get("status") or "unknown")
        if status == "succeeded":
            state, message = "complete", "历史交付可读"
            human = "quality_pass"
        elif status == "waiting_human_review":
            state = "waiting_review"
            message = "待人工复核" if availability == "review_available" else "仅保留溯源记录，文件在本地冷归档"
            human = "pending"
        elif status == "blocked":
            state, message, human = "machine_blocked", "历史运行已阻断，仅保留溯源记录", "invalid"
        else:
            state, message, human = "failed", "历史运行失败，仅保留溯源记录", "invalid"
        timestamp = _catalog_timestamp(run.get("finished_at") or run.get("started_at"))
        rows.append({
            "run_id": str(run.get("run_id") or ""),
            "parent_run_id": str(run.get("parent_run_id") or ""),
            "project_id": "",
            "state": state,
            "message": message,
            "updated_at": timestamp,
            "report": {
                "project_name": str(run.get("project_name") or "未命名历史项目"),
                "script_type": script_type,
                "status": status,
                "human_review_status": human,
                "project_hint": "精选历史归档",
            },
            "artifacts": artifacts,
            "stages": [],
            "active_stage": None,
            "brief": {},
            "artifact_availability": availability,
            "read_only_legacy": True,
        })
    return rows


def _catalog_timestamp(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _cloud_run_dto(run: dict) -> dict:
    return {
        "run_id": run.get("run_id"),
        "parent_run_id": run.get("parent_run_id"),
        "source_run_id": run.get("source_run_id"),
        "workspace_id": run.get("workspace_id"),
        "project_id": run.get("project_id"),
        "category_id": run.get("category_id"),
        "status": run.get("status"),
        "runner_version": run.get("runner_version"),
        "source_commit": run.get("source_commit"),
        "run_schema_version": run.get("run_schema_version"),
        "contract": run.get("contract"),
        "package_id": run.get("package_id"),
        "package_version": run.get("package_version"),
        "package_sha256": run.get("package_sha256"),
        "package_schema_version": run.get("package_schema_version"),
        "input_sha256": run.get("input_sha256"),
        "artifact_availability": run.get("artifact_availability"),
        "result_ref": run.get("result_ref"),
        "result_sha256": run.get("result_sha256"),
        "exit_code": run.get("exit_code"),
        "error_code": run.get("error_code"),
        "attempts": run.get("attempts"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "updated_at": run.get("updated_at"),
    }


def launch_payload_from_run(run_dir: str | Path) -> LaunchPayload:
    root = Path(run_dir)
    freeze_path = root / "input_freeze.json"
    if not freeze_path.is_file():
        raise ValueError("父版本缺少 input_freeze，不能恢复表单与模型配置")
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    brief = dict(freeze.get("brief") or {})
    config = dict(freeze.get("config") or {})
    if not brief:
        raise ValueError("父版本缺少 Brief")
    writer_provider = str(config.get("writer_provider") or "minimax-cli")
    editor_provider = str(config.get("editor_provider") or "minimax-cli")
    writer_model = str(config.get("writer_model") or config.get("text_model") or "MiniMax-M2.7")
    editor_model = str(config.get("editor_model") or config.get("text_model") or "MiniMax-M2.7")
    route = (writer_provider, writer_model, editor_provider, editor_model)
    if route == ("deepseek-http", "deepseek-v4-pro", "minimax-cli", "MiniMax-M3"):
        model_profile = "adaptive"
    elif route == ("minimax-cli", "MiniMax-M3", "minimax-cli", "MiniMax-M3"):
        model_profile = "minimax-m3"
    elif route == ("deepseek-http", "deepseek-v4-pro", "deepseek-http", "deepseek-v4-pro"):
        model_profile = "deepseek-only"
    elif route == (
        "deepseek-http",
        "deepseek-v4-pro",
        "minimax-cli",
        "MiniMax-M2.7-highspeed",
    ):
        model_profile = "dual-ab"
    else:
        model_profile = "current"
    options = LaunchOptions(
        source_root=str(config.get("source_root") or DEFAULT_SOURCE_ROOT),
        asset_package=str(config.get("asset_package") or ""),
        output_dir=str(DEFAULT_OUTPUT_ROOT),
        max_loops=int(config.get("max_loops") or 3),
        quality_profile=str(config.get("quality_profile") or "draft"),
        text_model=str(config.get("text_model") or "MiniMax-M2.7"),
        model_profile=model_profile,
        writer_provider=writer_provider,
        writer_model=writer_model,
        editor_provider=editor_provider,
        editor_model=editor_model,
        vision_model=str(config.get("vision_model") or ""),
        allow_cached_analysis=True,
    )
    return LaunchPayload(brief=brief_form_from_brief(brief), options=options)


def run_constraints(run_dir: str | Path) -> dict:
    for name in ("visual_binding.json", "script_draft.json", "script_plan.json"):
        path = Path(run_dir) / name
        if path.is_file():
            try:
                return dict(json.loads(path.read_text(encoding="utf-8")).get("constraints") or {})
            except (OSError, json.JSONDecodeError):
                pass
    return {}


def map_historical_review_returns(
    imported_issues: list[dict],
    review_issues: list[dict],
) -> dict[str, tuple[str, ...]]:
    """Map legacy shot, adjacent-edge and full-film failures to Formal issues."""
    by_shot: dict[str, list[str]] = {}
    issue_order: list[str] = []
    for item in review_issues:
        issue_key = str(item.get("issue_key") or "").strip()
        shot_no = str(item.get("shot_no") or "").strip()
        if not shot_no:
            continue
        by_shot.setdefault(shot_no, [])
        if issue_key:
            issue_order.append(issue_key)
            by_shot[shot_no].append(issue_key)
    returned_keys: set[str] = set()
    returned_shots: set[str] = set()
    for imported in imported_issues:
        slot = str(imported.get("slot_index") or "").strip()
        shot_no = str(imported.get("shot_no") or "").strip()
        targets: tuple[str, ...]
        if slot.startswith("full_film:"):
            targets = tuple(by_shot)
        elif slot.startswith("adjacent:"):
            match = re.fullmatch(r"adjacent:([^>:\s]+)->([^>:\s]+)", slot)
            if match is None:
                raise WorkspaceServiceError(
                    "REVIEW_IMPORT_MAPPING_UNSUPPORTED",
                    "历史相邻复核范围无法映射到当前镜头",
                )
            targets = (match.group(1), match.group(2))
        elif shot_no in by_shot:
            targets = (shot_no,)
        elif slot in by_shot:
            targets = (slot,)
        else:
            raise WorkspaceServiceError(
                "REVIEW_IMPORT_MAPPING_UNSUPPORTED",
                "历史人工复核包含当前版本无法识别的失败范围",
            )
        unmatched = [target for target in targets if target not in by_shot]
        if unmatched:
            raise WorkspaceServiceError(
                "REVIEW_IMPORT_MAPPING_UNSUPPORTED",
                "历史人工复核引用了当前版本不存在的镜头",
            )
        for target in targets:
            returned_shots.add(target)
            returned_keys.update(by_shot[target])
    return {
        "issue_keys": tuple(key for key in issue_order if key in returned_keys),
        "shot_nos": tuple(sorted(returned_shots, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))),
    }


def _start_feedback_revision(
    jobs: JobManager,
    manifest_path: Path,
    payload: FeedbackPayload,
    parent_payload: LaunchPayload,
    *,
    prior_constraints_path: str | Path | None = None,
    prior_constraints: dict | None = None,
) -> dict:
    if payload.category == "image" and not payload.shot_no.strip():
        raise HTTPException(status_code=422, detail="图片问题必须填写镜号")
    if payload.supplemental_image_path:
        image = Path(payload.supplemental_image_path).expanduser().resolve()
        if not image.is_file() or image.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise HTTPException(status_code=422, detail="补充图片不存在或格式不支持")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    parent_run_id = str(manifest.get("run_id") or "")
    if not parent_run_id:
        raise HTTPException(status_code=409, detail="父版本缺少 run_id")
    feedback = store_user_feedback(
        manifest_path.parent,
        run_id=parent_run_id,
        prior_constraints_path=prior_constraints_path,
        prior_constraints=prior_constraints,
        **payload.model_dump(),
    )
    options = parent_payload.options.model_copy(update={
        "resume_run_id": "",
        "from_stage": feedback["restart_stage"],
        "retry_stage": "",
        "revision_constraints": feedback["constraints_path"],
        "parent_run_id": parent_run_id,
        "allow_cached_analysis": True,
    })
    child = jobs.start(parent_payload.model_copy(update={"options": options}))
    return {"feedback": feedback, "child_job": child.snapshot()}


def choose_local_path(kind: Literal["brief", "image", "review", "asset", "media", "source", "output", "package"]) -> str | None:
    if sys.platform != "darwin":
        raise RuntimeError("当前文件选择器仅支持 macOS")
    if kind == "brief":
        script = 'POSIX path of (choose file with prompt "选择 Brief Excel 或 TXT")'
    elif kind == "image":
        script = 'POSIX path of (choose file with prompt "选择补充参考图片")'
    elif kind == "review":
        script = 'POSIX path of (choose file with prompt "选择已填写的人工复核 Excel")'
    elif kind == "asset":
        script = 'POSIX path of (choose file with prompt "选择待导入的 PNG/JPG/WEBP 或 XLSX")'
    else:
        prompts = {
            "media": "选择项目图片/视频目录",
            "source": "选择六类别资料库目录",
            "output": "选择输出目录",
            "package": "选择 Loomtake 资产包目录",
        }
        script = f'POSIX path of (choose folder with prompt "{prompts[kind]}")'
    completed = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        if "User canceled" in completed.stderr:
            return None
        raise RuntimeError(completed.stderr.strip() or "无法打开文件选择器")
    return completed.stdout.strip().rstrip("/")


def create_app(
    *,
    agent_api: ReadonlyAgentService | ExecutableAgentService | None = None,
    ui_workbench: UiWorkbenchService | None = None,
    ui_governance: UiGovernanceService | None = None,
    formal_brief_adapter: FormalBriefInputAdapter | None = None,
    formal_brief_upload_factory: FormalBriefUploadFactory | None = None,
    formal_generation_service: FormalGenerationService | None = None,
    formal_script_service: FormalScriptWorkspaceService | None = None,
    formal_image_service: FormalImageWorkspaceService | None = None,
    formal_review_service: FormalReviewWorkspaceService | None = None,
    formal_admin_service: FormalAdminService | None = None,
    cloud_run_repository=None,
    cloud_storage=None,
) -> FastAPI:
    app = FastAPI(title="Loomtake 脚本工作台", docs_url=None, redoc_url=None)
    if AUTH_MODE == "github":
        from .cloud_runtime import PostgresCloudRunRepository
        from .github_oauth import (
            FLOW_COOKIE,
            SESSION_COOKIE,
            GithubOAuthSettings,
            authorization_request,
            complete_authorization,
            session_sha256,
        )

        oauth_settings = GithubOAuthSettings.from_environment()
        oauth_repository = PostgresCloudRunRepository.from_environment()
        oauth_repository.check_schema()

        @app.middleware("http")
        async def require_github_session(request: Request, call_next):
            path = request.url.path
            if request.method == "POST" and path in {
                "/api/formal/owner",
                "/api/agent/owner/enable",
                "/api/agent/owner/reissue",
            }:
                return JSONResponse(
                    {"detail": "GitHub 模式禁止初始化本机 owner"},
                    status_code=409,
                )
            if (
                request.method == "OPTIONS"
                or path.startswith("/auth/github/")
            ):
                return await call_next(request)
            if validated_formal_agent_header(
                agent_api or get_agent_api(),
                path,
                str(request.headers.get("X-MPlus-Agent-Auth") or ""),
            ):
                return await call_next(request)
            token = str(request.cookies.get(SESSION_COOKIE) or "")
            user = None
            if token:
                try:
                    user = oauth_repository.validate_web_session(session_sha256(token))
                except RuntimeError:
                    user = None
            if user:
                request.state.github_user = user
                return await call_next(request)
            if path.startswith("/api/"):
                return JSONResponse({"detail": "GitHub OAuth 会话缺失或已过期"}, status_code=401)
            return RedirectResponse("/auth/github/login", status_code=303)

        @app.get("/auth/github/login")
        def github_login() -> Response:
            target, flow_cookie = authorization_request(oauth_settings)
            response = RedirectResponse(target, status_code=303)
            response.set_cookie(
                FLOW_COOKIE,
                flow_cookie,
                max_age=600,
                secure=True,
                httponly=True,
                samesite="lax",
                path="/auth/github/callback",
            )
            return response

        @app.get("/auth/github/callback")
        def github_callback(
            code: str = "",
            state: str = "",
            mplus_github_oauth_flow: str = Cookie(default=""),
        ) -> Response:
            try:
                session_token, _identity = complete_authorization(
                    oauth_settings,
                    oauth_repository,
                    code=code,
                    state=state,
                    flow_cookie=mplus_github_oauth_flow,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            response = RedirectResponse("/", status_code=303)
            response.delete_cookie(FLOW_COOKIE, path="/auth/github/callback")
            response.set_cookie(
                SESSION_COOKIE,
                session_token,
                max_age=8 * 60 * 60,
                secure=True,
                httponly=True,
                samesite="lax",
                path="/",
            )
            return response

        @app.post("/auth/github/logout")
        def github_logout(request: Request) -> Response:
            token = str(request.cookies.get(SESSION_COOKIE) or "")
            if token:
                oauth_repository.revoke_web_session(session_sha256(token))
            response = RedirectResponse("/auth/github/login", status_code=303)
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(ALLOWED_HOSTS))
    app.state.session_token = secrets.token_urlsafe(32)
    runtime_for_artifacts = load_runtime_config()
    if cloud_storage is None and os.environ.get("MPLUS_COS_BUCKET"):
        from .cloud_storage import CosObjectStorage

        cloud_storage = CosObjectStorage.from_environment()
    app.state.cloud_storage = cloud_storage
    app.state.artifacts = ArtifactRegistry(
        cloud_storage=cloud_storage,
        cache_root=Path(runtime_for_artifacts["cache_root"]) / "remote-artifacts",
    )
    app.state.jobs = JobManager(app.state.artifacts)
    app.state.cloud_run_repository = cloud_run_repository
    app.state.agent_api = agent_api
    app.state.ui_workbench = ui_workbench
    app.state.ui_governance = ui_governance
    app.state.formal_brief_enabled = bool(
        agent_api is not None and formal_brief_adapter is not None
        and formal_brief_upload_factory is not None
    )
    app.state.formal_generation_service = formal_generation_service
    app.state.formal_script_service = formal_script_service
    app.state.formal_image_service = formal_image_service
    app.state.formal_review_service = formal_review_service
    app.state.formal_admin_service = formal_admin_service
    if (formal_brief_adapter is not None or formal_brief_upload_factory is not None) and agent_api is None:
        raise ValueError("formal Brief 组合入口必须显式传入同一 agent_api")

    def get_cloud_run_repository():
        if app.state.cloud_run_repository is None:
            try:
                from .cloud_runtime import PostgresCloudRunRepository

                app.state.cloud_run_repository = PostgresCloudRunRepository.from_environment()
                app.state.cloud_run_repository.check_schema()
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        return app.state.cloud_run_repository

    def _read_app_history(limit: int | None = 300, *, run_id: str | None = None) -> list[dict]:
        cloud_legacy_runs = []
        if app.state.cloud_run_repository is not None or os.environ.get("MPLUS_POSTGRES_DSN"):
            cloud_legacy_runs = get_cloud_run_repository().list_legacy_runs(limit=None, run_id=run_id)
        rows = scan_history(
            app.state.artifacts,
            limit=limit,
            legacy_runs=cloud_legacy_runs, run_id=run_id,
        )
        with get_agent_api().services.store.connection() as connection:
            bindings = {
                row["run_id"]: row
                for row in connection.execute(
                    "SELECT run_id, project_id, parent_run_id FROM project_runs"
                ).fetchall()
            }
        projected = []
        for value in rows:
            item = dict(value)
            binding = bindings.get(str(item.get("run_id") or ""))
            if binding is not None:
                physical_project_id = str(item.get("project_id") or "")
                if physical_project_id and physical_project_id != binding["project_id"]:
                    item["project_id"] = ""
                    item["identity_status"] = "binding_conflict"
                else:
                    item["project_id"] = binding["project_id"]
                    item["parent_run_id"] = str(
                        item.get("parent_run_id") or binding["parent_run_id"] or ""
                    )
                    item["identity_status"] = "workspace_bound"
            projected.append(item)
        return projected

    history_reads: dict[tuple, Future] = {}
    history_reads_lock = threading.Lock()

    def app_history(limit: int | None = 300, *, run_id: str | None = None) -> list[dict]:
        # Share only overlapping scans. Each handler still authorizes its own
        # rows; completed scans are discarded so later reads see file changes.
        key = (limit, run_id)
        with history_reads_lock:
            pending = history_reads.get(key)
            owns_scan = pending is None
            if owns_scan:
                pending = history_reads[key] = Future()
        if not owns_scan:
            return deepcopy(pending.result())
        try:
            rows = _read_app_history(limit, run_id=run_id)
            pending.set_result(rows)
            return deepcopy(rows)
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with history_reads_lock:
                if history_reads.get(key) is pending:
                    del history_reads[key]

    def get_agent_api() -> ReadonlyAgentService | ExecutableAgentService:
        if app.state.agent_api is None:
            runtime = load_runtime_config()
            readonly = ReadonlyAgentService.from_runtime_config(runtime)
            brief_store = BriefArtifactStore(runtime["data_root"])
            app.state.agent_api = ExecutableAgentService(
                ExecutableAgentServices(
                    readonly=readonly,
                    projects=readonly.services.projects,
                    tasks=readonly.services.tasks,
                    confirmations=ActionConfirmationService(
                        readonly.services.store,
                        load_or_create_confirmation_key(runtime["data_root"]),
                    ),
                    idempotency=ToolIdempotencyService(readonly.services.store),
                    brief_store=brief_store,
                    backend=LocalJobExecutionBackend(
                        app.state.jobs,
                        app.state.artifacts,
                        readonly.services.artifacts,
                        brief_store,
                        runtime,
                    ),
                    delivery=VersionDeliveryService(
                        readonly.services.store,
                        readonly.services.artifacts,
                    ),
                )
            )
        return app.state.agent_api

    def get_ui_workbench() -> UiWorkbenchService:
        if app.state.ui_workbench is None:
            runtime = load_runtime_config()
            signing_key = load_or_create_confirmation_key(runtime["data_root"])
            store = WorkspaceStore.from_runtime_config(runtime)
            assets = AssetCenterToolService.from_runtime_config(
                confirmation_signing_key=signing_key,
                config=runtime,
            )
            app.state.ui_workbench = UiWorkbenchService(
                UiWorkbenchServices(
                    versions=VersionWorkbenchService.from_runtime_config(
                        confirmation_signing_key=signing_key,
                        config=runtime,
                    ),
                    assets=assets,
                    manual_versions=ManualVersionCommandService(
                        ManualVersionCommandServices(
                            store=store,
                            projects=ProjectService(store),
                            versions=VersionService(store),
                            artifacts=RunArtifactResolver(runtime["runs_root"]),
                            publication_projection=VersionPublicationProjectionService(
                                store, RunArtifactResolver(runtime["runs_root"]),
                            ),
                        )
                    ),
                    manual_asset_publish=ManualAssetPublishCommandService(
                        ManualAssetPublishCommandServices(
                            store=store,
                            projects=ProjectService(store),
                            packages=assets.services.packages,
                        )
                    ),
                )
            )
        return app.state.ui_workbench

    def get_ui_governance() -> UiGovernanceService:
        if app.state.ui_governance is None:
            runtime = load_runtime_config()
            signing_key = load_or_create_confirmation_key(runtime["data_root"])
            strategies = CreativeStrategyToolService.from_runtime_config(
                confirmation_signing_key=signing_key,
                config=runtime,
            )
            knowledge = KnowledgeToolService.from_runtime_config(
                confirmation_signing_key=signing_key,
                config=runtime,
            )
            store = knowledge.services.store
            app.state.ui_governance = UiGovernanceService(
                UiGovernanceServices(
                    strategies=strategies,
                    knowledge=knowledge,
                    version_reader=get_ui_workbench().services.versions,
                    identity=LocalIdentityAdapter(strategies.services.store),
                    manual_knowledge_approval=ManualKnowledgeApprovalCommandService(
                        ManualKnowledgeApprovalCommandServices(
                            store=store,
                            projects=ProjectService(store),
                            knowledge=KnowledgeService(store),
                        )
                    ),
                )
            )
        return app.state.ui_governance

    def agent_error(exc: Exception) -> HTTPException:
        code = str(getattr(exc, "code", "INTERNAL_ERROR"))
        message = str(getattr(exc, "message", "") or str(exc) or "脚本助手请求未完成")
        if code in {"AUTH_SESSION_INVALID"}:
            status_code = 401
        elif code == "PERMISSION_DENIED":
            status_code = 403
        elif code == "NOT_FOUND":
            status_code = 404
        elif code == "INVALID_INPUT":
            status_code = 422
        else:
            status_code = 409
        return HTTPException(
            status_code=status_code,
            detail={"code": code, "message": message},
        )

    def verify_mutation(
        request: Request,
        x_mplus_ui_token: str = Header(default=None),
    ) -> None:
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            raise HTTPException(status_code=403, detail="拒绝非本地页面请求")
        if not secrets.compare_digest(x_mplus_ui_token or "", app.state.session_token):
            raise HTTPException(status_code=403, detail="本地会话已失效，请刷新页面")

    @app.get("/api/health")
    def health() -> dict:
        return {"service": "mplus-launch-ui", "status": "ok", "ui_build_id": ui_build_id()}

    @app.get("/api/ui/session")
    def ui_session(request: Request) -> dict:
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            raise HTTPException(status_code=403, detail="拒绝非本地页面请求")
        return JSONResponse({"token": app.state.session_token}, headers={"Cache-Control": "no-store"})

    @app.get("/api/bootstrap")
    def bootstrap() -> dict:
        runtime_config = load_runtime_config()
        configured_asset_packages = dict(runtime_config.get("asset_packages") or {})
        legacy_asset_package = str(
            runtime_config.get("asset_package", DEFAULT_ASSET_PACKAGE) or ""
        ).strip()
        if legacy_asset_package and "发布会" not in configured_asset_packages:
            configured_asset_packages["发布会"] = legacy_asset_package
        preferred_model_profile = str(runtime_config.get("model_profile") or "").strip()
        if preferred_model_profile not in {
            "adaptive",
            "minimax-m3",
            "deepseek-only",
            "current",
            "dual-ab",
        }:
            preferred_model_profile = (
                "adaptive"
                if os.environ.get("DEEPSEEK_API_KEY")
                else "current"
            )
        asset_index_stats = {}
        for script_type, package_path in configured_asset_packages.items():
            try:
                asset_index_stats[script_type] = asset_package_index_summary(package_path)
            except (AssetPackageError, OSError, sqlite3.Error, json.JSONDecodeError):
                asset_index_stats[script_type] = {"error": "资产包索引不可读取"}
        return {
            "token": app.state.session_token,
            "defaults": {
                "source_root": str(DEFAULT_SOURCE_ROOT),
                "output_dir": str(DEFAULT_OUTPUT_ROOT),
                "max_loops": 3,
                "quality_profile": "client-review",
                "text_model": "MiniMax-M2.7",
                "asset_package": configured_asset_packages.get("发布会", ""),
                "asset_packages": configured_asset_packages,
                "model_profile": preferred_model_profile,
            },
            "brief_presets": BRIEF_PRESETS,
            "asset_index_stats": asset_index_stats,
            "system_checks": system_checks(),
            "capabilities": {
                "formal_brief": app.state.formal_brief_enabled,
            },
        }

    @app.get("/api/evolution")
    def evolution_summary() -> dict:
        return summarize_evolution_ledgers(
            DEFAULT_ASSET_CACHE_ROOT.glob("*/evolution_ledger.jsonl")
        )

    @app.get("/api/manual/session", dependencies=[Depends(verify_mutation)])
    def manual_session(x_mplus_manual_auth: str = Header(default="")) -> dict:
        try:
            return get_agent_api().services.identity.validate_session(x_mplus_manual_auth)
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.get("/api/agent/setup")
    def agent_setup() -> dict:
        return get_agent_api().setup_status()

    @app.post("/api/agent/owner/enable", dependencies=[Depends(verify_mutation)])
    def enable_agent_owner(payload: AgentOwnerEnablePayload) -> dict:
        try:
            return get_agent_api().enable_local_owner(
                workspace_name=payload.workspace_name,
                display_name=payload.display_name,
                confirmed=payload.confirmed,
            )
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post("/api/agent/owner/reissue", dependencies=[Depends(verify_mutation)])
    def reissue_agent_owner(payload: AgentOwnerRecoveryPayload) -> dict:
        public_host = urlsplit(PUBLIC_URL).hostname
        if HOST not in {"127.0.0.1", "localhost", "::1"} or public_host not in {
            "127.0.0.1", "localhost", "::1",
        }:
            raise HTTPException(
                status_code=403,
                detail="本机管理员会话只能在本机监听模式重新签发",
            )
        try:
            return get_agent_api().reissue_local_owner_session(
                confirmed=payload.confirmed,
            )
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post("/api/agent/projects/onboard", dependencies=[Depends(verify_mutation)])
    def onboard_agent_project(
        payload: AgentOnboardProjectPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        api = get_agent_api()
        if not isinstance(api, ExecutableAgentService):
            raise HTTPException(status_code=409, detail="E03 可执行能力未启用")
        try:
            brief = payload.brief.to_brief()
            return api.onboard_project(
                x_mplus_agent_auth,
                workspace_id=payload.workspace_id,
                expected_workspace_version=payload.expected_workspace_version,
                brief=brief,
                category_id=category_id_for_script_type(brief["脚本类型"]),
                confirmed=payload.confirmed,
            )
        except (WorkspaceServiceError, ArtifactResolutionError, ValueError) as exc:
            if isinstance(exc, ValueError):
                exc = WorkspaceServiceError("INVALID_INPUT", str(exc))
            raise agent_error(exc) from exc

    @app.get("/api/agent/projects")
    def agent_projects(
        workspace_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return {
                "items": get_agent_api().list_projects(
                    x_mplus_agent_auth, workspace_id
                )
            }
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.get("/api/agent/projects/{project_id}/state")
    def agent_project_state(
        project_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_agent_api().get_project_state(
                x_mplus_agent_auth, project_id
            )
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post("/api/agent/sessions", dependencies=[Depends(verify_mutation)])
    def create_agent_session(
        payload: AgentSessionPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_agent_api().create_session(
                x_mplus_agent_auth, payload.project_id
            )
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.get("/api/agent/sessions/{agent_session_id}")
    def get_agent_session(
        agent_session_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_agent_api().get_session(
                x_mplus_agent_auth, agent_session_id
            )
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/sessions/{agent_session_id}/messages",
        dependencies=[Depends(verify_mutation)],
    )
    def submit_agent_message(
        agent_session_id: str,
        payload: AgentMessagePayload,
        background_tasks: BackgroundTasks,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            if payload.context is not None:
                agent = get_agent_api()
                if not hasattr(agent, 'script_revision'):
                    raise WorkspaceServiceError('FORMAL_ADVISOR_UNAVAILABLE', '当前工作台助手未接入')
                result = agent.start_workbench(x_mplus_agent_auth, agent_session_id, content=payload.content,
                    context=payload.context.model_dump(), operation_key=payload.operation_key)
                if result['task']['status'] == 'understanding':
                    background_tasks.add_task(agent.execute_workbench, x_mplus_agent_auth, result['task']['task_id'])
                return result
            return get_agent_api().submit_message(
                x_mplus_agent_auth,
                agent_session_id,
                content=payload.content,
                run_id=payload.run_id,
            )
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.get('/api/agent/workbench/{task_id}')
    def read_workbench(task_id: str, x_mplus_agent_auth: str = Header(default='')) -> dict:
        try:
            return get_agent_api().read_workbench(x_mplus_agent_auth, task_id)
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post('/api/agent/workbench/{task_id}/confirm', dependencies=[Depends(verify_mutation)])
    def confirm_workbench(task_id: str, payload: WorkbenchConfirmPayload,
                          x_mplus_agent_auth: str = Header(default='')) -> dict:
        try:
            return get_agent_api().confirm_workbench(x_mplus_agent_auth, task_id, **payload.model_dump())
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post('/api/agent/workbench/{task_id}/undo', dependencies=[Depends(verify_mutation)])
    def undo_workbench(task_id: str, x_mplus_agent_auth: str = Header(default='')) -> dict:
        try:
            return get_agent_api().undo_workbench(x_mplus_agent_auth, task_id)
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/sessions/{agent_session_id}/brief-analysis",
        dependencies=[Depends(verify_mutation)],
    )
    def analyze_agent_brief(
        agent_session_id: str,
        payload: BriefAnalysisPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            service = get_agent_api()
            analyze = getattr(service, "analyze_brief", None)
            if analyze is None:
                raise WorkspaceServiceError("FORMAL_ADVISOR_UNAVAILABLE", "需求分析尚未接入")
            return analyze(x_mplus_agent_auth, agent_session_id, content=payload.content)
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post("/api/agent/sessions/{agent_session_id}/brief-answers", dependencies=[Depends(verify_mutation)])
    def answer_agent_brief(agent_session_id: str, payload: BriefQuestionAnswerPayload,
                           x_mplus_agent_auth: str = Header(default="")) -> dict:
        try:
            return get_agent_api().answer_brief_question(x_mplus_agent_auth, agent_session_id, **payload.model_dump())
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.get("/api/agent/tasks/{task_id}/events")
    def agent_task_events(
        task_id: str,
        after_sequence: int = 0,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return {
                "items": get_agent_api().list_task_events(
                    x_mplus_agent_auth,
                    task_id,
                    after_sequence=max(0, after_sequence),
                )
            }
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.get("/api/agent/tasks/{task_id}")
    def agent_task(
        task_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        api = get_agent_api()
        if not isinstance(api, ExecutableAgentService):
            raise HTTPException(status_code=409, detail="E03 可执行能力未启用")
        try:
            return api.sync_task(x_mplus_agent_auth, task_id)
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/tasks/{task_id}/confirm",
        dependencies=[Depends(verify_mutation)],
    )
    def confirm_agent_task(
        task_id: str,
        payload: AgentConfirmPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        api = get_agent_api()
        if not isinstance(api, ExecutableAgentService):
            raise HTTPException(status_code=409, detail="E03 可执行能力未启用")
        try:
            return api.confirm_action(
                x_mplus_agent_auth,
                task_id,
                proposal_digest=payload.proposal_digest,
            )
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/tasks/{task_id}/image-selection",
        dependencies=[Depends(verify_mutation)],
    )
    def select_agent_image(
        task_id: str,
        payload: AgentImageSelectionPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        api = get_agent_api()
        if not isinstance(api, FormalExecutableAgentService):
            raise HTTPException(status_code=409, detail="Formal 图片智能体未启用")
        try:
            return api.select_image_candidate(
                x_mplus_agent_auth, task_id,
                candidate_key=payload.candidate_key,
                reuse_reason=payload.reuse_reason,
            )
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/tasks/{task_id}/temporary-text-basis",
        dependencies=[Depends(verify_mutation)],
    )
    def select_agent_temporary_text_basis(
        task_id: str,
        payload: AgentTemporaryTextBasisPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        api = get_agent_api()
        if not isinstance(api, FormalExecutableAgentService):
            raise HTTPException(status_code=409, detail="Formal 图片智能体未启用")
        try:
            return api.select_temporary_text_basis(
                x_mplus_agent_auth, task_id, reason=payload.reason,
            )
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/tasks/{task_id}/dismiss",
        dependencies=[Depends(verify_mutation)],
    )
    def dismiss_agent_task(
        task_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        api = get_agent_api()
        if not isinstance(api, ExecutableAgentService):
            raise HTTPException(status_code=409, detail="E03 可执行能力未启用")
        try:
            return api.dismiss_proposal(x_mplus_agent_auth, task_id)
        except (WorkspaceServiceError, ArtifactResolutionError) as exc:
            raise agent_error(exc) from exc

    @app.get(
        "/api/agent/workbench/versions",
        dependencies=[Depends(verify_mutation)],
    )
    def workbench_versions(
        project_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_workbench().version_tree(
                x_mplus_agent_auth, project_id
            )
        except (
            WorkspaceServiceError,
            ArtifactResolutionError,
            VersionWorkbenchRecoveryRequired,
        ) as exc:
            raise agent_error(exc) from exc

    @app.get(
        "/api/agent/workbench/versions/read",
        dependencies=[Depends(verify_mutation)],
    )
    def workbench_read_version(
        project_id: str,
        version_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_workbench().read_version(
                x_mplus_agent_auth,
                project_id=project_id,
                version_id=version_id,
            )
        except (
            WorkspaceServiceError,
            ArtifactResolutionError,
            VersionWorkbenchRecoveryRequired,
        ) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/workbench/versions/compare",
        dependencies=[Depends(verify_mutation)],
    )
    def workbench_compare_versions(
        payload: WorkbenchVersionComparePayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_workbench().compare_versions(
                x_mplus_agent_auth,
                project_id=payload.project_id,
                left_version_id=payload.left_version_id,
                right_version_id=payload.right_version_id,
            )
        except (
            WorkspaceServiceError,
            ArtifactResolutionError,
            VersionWorkbenchRecoveryRequired,
            ValueError,
        ) as exc:
            if isinstance(exc, ValueError) and not hasattr(exc, "code"):
                exc = WorkspaceServiceError("INVALID_INPUT", str(exc))
            raise agent_error(exc) from exc

    @app.post(
        "/api/manual/versions/actions/prepare",
        dependencies=[Depends(verify_mutation)],
    )
    def workbench_prepare_version_action(
        payload: ManualVersionActionPayload,
        x_mplus_manual_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_workbench().prepare_version_action(
                x_mplus_manual_auth,
                tool_name=payload.tool_name,
                parameters=payload.parameters,
            )
        except (
            WorkspaceServiceError,
            ArtifactResolutionError,
            VersionWorkbenchRecoveryRequired,
        ) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/manual/versions/actions/execute",
        dependencies=[Depends(verify_mutation)],
    )
    def workbench_execute_version_action(
        payload: ManualVersionActionPayload,
        x_mplus_manual_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_workbench().execute_version_action(
                x_mplus_manual_auth,
                tool_name=payload.tool_name,
                parameters=payload.parameters,
                command_token=payload.command_token,
            )
        except (
            WorkspaceServiceError,
            ArtifactResolutionError,
            VersionWorkbenchRecoveryRequired,
        ) as exc:
            raise agent_error(exc) from exc

    @app.get(
        "/api/agent/workbench/assets",
        dependencies=[Depends(verify_mutation)],
    )
    def workbench_assets(
        agent_session_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_workbench().asset_context(
                x_mplus_agent_auth,
                agent_session_id=agent_session_id,
            )
        except (WorkspaceServiceError, AssetToolRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/workbench/assets/freeze",
        dependencies=[Depends(verify_mutation)],
    )
    def workbench_freeze_asset(
        payload: WorkbenchAssetFreezePayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_workbench().freeze_asset(
                x_mplus_agent_auth,
                agent_session_id=payload.agent_session_id,
                source=payload.path,
            )
        except (WorkspaceServiceError, AssetToolRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.post("/api/agent/workbench/assets/upload", dependencies=[Depends(verify_mutation)])
    async def workbench_upload_asset(request: Request, agent_session_id: str,
                                     x_mplus_agent_auth: str = Header(default="")) -> dict:
        from starlette.concurrency import run_in_threadpool
        from starlette.formparsers import MultiPartException, MultiPartParser
        from .input_limits import FORMAL_UPLOAD_FILE_BYTES
        try:
            workbench = get_ui_workbench()
            # Check the existing project session before consuming any upload bytes.
            workbench.services.assets._session(x_mplus_agent_auth,
                agent_session_id=agent_session_id, permission="asset.import")
            async def bounded_body():
                total = 0
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > FORMAL_UPLOAD_FILE_BYTES + 65536:
                        raise MultiPartException("上传请求超过单文件5 MiB限制")
                    yield chunk
            parser = MultiPartParser(request.headers, bounded_body(), max_files=1,
                                     max_fields=0, max_part_size=FORMAL_UPLOAD_FILE_BYTES)
            form = await parser.parse()
            try:
                entries = form.multi_items()
                if len(entries) != 1 or entries[0][0] != "file":
                    raise WorkspaceServiceError("INVALID_INPUT", "请上传一个file文件")
                item = entries[0][1]
                if not getattr(item, "filename", None) or not hasattr(item, "file"):
                    raise WorkspaceServiceError("INVALID_INPUT", "上传文件须有文件名和内容")
                return await run_in_threadpool(workbench.freeze_uploaded_asset,
                    x_mplus_agent_auth, agent_session_id=agent_session_id,
                    filename=item.filename, stream=item.file)
            finally:
                await form.close()
        except MultiPartException as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (WorkspaceServiceError, AssetToolRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/workbench/assets/actions/prepare",
        dependencies=[Depends(verify_mutation)],
    )
    def workbench_prepare_asset_action(
        payload: WorkbenchAssetActionPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_workbench().prepare_asset_action(
                x_mplus_agent_auth,
                agent_session_id=payload.agent_session_id,
                tool_name=payload.tool_name,
                parameters=payload.parameters,
            )
        except (WorkspaceServiceError, AssetToolRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/workbench/assets/actions/execute",
        dependencies=[Depends(verify_mutation)],
    )
    def workbench_execute_asset_action(
        payload: WorkbenchAssetActionPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        if not payload.confirmation_token:
            raise HTTPException(status_code=422, detail="缺少动作确认令牌")
        try:
            return get_ui_workbench().execute_asset_action(
                x_mplus_agent_auth,
                agent_session_id=payload.agent_session_id,
                tool_name=payload.tool_name,
                parameters=payload.parameters,
                confirmation_token=payload.confirmation_token,
            )
        except (WorkspaceServiceError, AssetToolRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/manual/assets/publish/prepare",
        dependencies=[Depends(verify_mutation)],
    )
    def manual_prepare_asset_publish(
        payload: ManualAssetPublishPreparePayload,
        x_mplus_manual_auth: str = Header(default=""),
    ) -> dict:
        try:
            prepared = get_ui_workbench().prepare_asset_publish(
                x_mplus_manual_auth,
                project_id=payload.project_id,
                parameters=payload.model_dump(exclude={"project_id"}),
            )
            return {
                **prepared,
                "action": "publish_asset_import",
                "calls_models": False,
                "will_change": "发布已审核资产并允许项目检索",
                "impact_scope": f"项目 {payload.project_id} 的资产导入记录",
            }
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/manual/assets/publish/execute",
        dependencies=[Depends(verify_mutation)],
    )
    def manual_execute_asset_publish(
        payload: ManualCommandExecutePayload,
        x_mplus_manual_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_workbench().execute_asset_publish(
                x_mplus_manual_auth,
                project_id=payload.project_id,
                parameters=payload.parameters,
                command_token=payload.command_token,
            )
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/workbench/assets/search",
        dependencies=[Depends(verify_mutation)],
    )
    def workbench_search_assets(
        payload: WorkbenchAssetSearchPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_workbench().search_assets(
                x_mplus_agent_auth,
                agent_session_id=payload.agent_session_id,
                category_id=payload.category_id,
                query=payload.query,
                kind=payload.kind,
            )
        except (WorkspaceServiceError, AssetToolRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.get(
        "/api/agent/governance/strategies",
        dependencies=[Depends(verify_mutation)],
    )
    def governance_strategies(
        agent_session_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_governance().strategy_context(
                x_mplus_agent_auth,
                agent_session_id=agent_session_id,
            )
        except (
            WorkspaceServiceError,
            CreativeStrategyRecoveryRequired,
        ) as exc:
            raise agent_error(exc) from exc

    @app.post("/api/agent/governance/strategies/analyze", dependencies=[Depends(verify_mutation)])
    def governance_analyze_strategies(payload: GovernanceStrategyAnalyzePayload,
                                     x_mplus_agent_auth: str = Header(default="")) -> dict:
        try:
            return get_ui_governance().analyze_strategy_context(x_mplus_agent_auth,
                agent_session_id=payload.agent_session_id)
        except (WorkspaceServiceError, CreativeStrategyRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/governance/strategies/prepare",
        dependencies=[Depends(verify_mutation)],
    )
    def governance_prepare_strategy(
        payload: GovernanceStrategyPreparePayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_governance().prepare_strategy_selection(
                x_mplus_agent_auth,
                agent_session_id=payload.agent_session_id,
                strategy_portfolio_ref=payload.strategy_portfolio_ref,
                strategy_id=payload.strategy_id,
                idempotency_key=payload.idempotency_key,
            )
        except (
            WorkspaceServiceError,
            CreativeStrategyRecoveryRequired,
        ) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/governance/strategies/execute",
        dependencies=[Depends(verify_mutation)],
    )
    def governance_execute_strategy(
        payload: GovernanceActionExecutePayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_governance().execute_strategy_selection(
                x_mplus_agent_auth,
                agent_session_id=payload.agent_session_id,
                parameters=payload.parameters,
                confirmation_token=payload.confirmation_token,
            )
        except (
            WorkspaceServiceError,
            CreativeStrategyRecoveryRequired,
        ) as exc:
            raise agent_error(exc) from exc

    @app.get("/api/agent/governance/knowledge/evidence", dependencies=[Depends(verify_mutation)])
    def governance_knowledge_evidence(agent_session_id: str, x_mplus_agent_auth: str = Header(default="")) -> dict:
        try:
            return get_ui_governance().evidence_options(x_mplus_agent_auth, agent_session_id=agent_session_id)
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.get(
        "/api/agent/governance/knowledge",
        dependencies=[Depends(verify_mutation)],
    )
    def governance_knowledge(
        agent_session_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_governance().knowledge_context(
                x_mplus_agent_auth,
                agent_session_id=agent_session_id,
            )
        except (WorkspaceServiceError, KnowledgeToolRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/governance/knowledge/candidates/prepare",
        dependencies=[Depends(verify_mutation)],
    )
    def governance_prepare_knowledge_candidate(
        payload: GovernanceKnowledgeCandidatePayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_governance().prepare_knowledge_candidate(
                x_mplus_agent_auth,
                agent_session_id=payload.agent_session_id,
                scope=payload.scope,
                statement=payload.statement,
                evidence_refs=payload.evidence_refs,
                evidence_ids=payload.evidence_ids,
                idempotency_key=payload.idempotency_key,
            )
        except (WorkspaceServiceError, KnowledgeToolRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.post(
        "/api/agent/governance/knowledge/candidates/execute",
        dependencies=[Depends(verify_mutation)],
    )
    def governance_execute_knowledge_candidate(
        payload: GovernanceActionExecutePayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_governance().execute_knowledge_candidate(
                x_mplus_agent_auth,
                agent_session_id=payload.agent_session_id,
                parameters=payload.parameters,
                confirmation_token=payload.confirmation_token,
            )
        except (WorkspaceServiceError, KnowledgeToolRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.post("/api/manual/knowledge/approvals/prepare", dependencies=[Depends(verify_mutation)])
    def governance_prepare_knowledge_approval(
        payload: GovernanceKnowledgeApprovalPayload,
        x_mplus_manual_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_governance().prepare_manual_knowledge_approval(
                x_mplus_manual_auth,
                project_id=payload.project_id,
                parameters={
                    "candidate_id": payload.candidate_id,
                    "review_evidence": payload.review_evidence,
                    "idempotency_key": payload.idempotency_key,
                },
            )
        except (WorkspaceServiceError, KnowledgeToolRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.post("/api/manual/knowledge/approvals/execute", dependencies=[Depends(verify_mutation)])
    def governance_execute_knowledge_approval(
        payload: ManualCommandExecutePayload,
        x_mplus_manual_auth: str = Header(default=""),
    ) -> dict:
        try:
            return get_ui_governance().execute_manual_knowledge_approval(
                x_mplus_manual_auth,
                project_id=payload.project_id,
                parameters=payload.parameters,
                command_token=payload.command_token,
            )
        except (WorkspaceServiceError, KnowledgeToolRecoveryRequired) as exc:
            raise agent_error(exc) from exc

    @app.post("/api/preflight", dependencies=[Depends(verify_mutation)])
    def preflight(payload: LaunchPayload) -> dict:
        return preflight_response(preflight_payload(payload))

    @app.post("/api/import-brief", dependencies=[Depends(verify_mutation)])
    def import_brief(payload: PathPayload) -> dict:
        path = Path(payload.path).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in {".xlsx", ".txt"}:
            raise HTTPException(status_code=422, detail="请选择 .xlsx 或 .txt Brief")
        try:
            if path.suffix.lower() == ".xlsx":
                briefs = read_briefs(path)
                if len(briefs) != 1:
                    raise ValueError(f"一次只能导入一条 Brief，当前有 {len(briefs)} 条")
                brief = briefs[0]
            else:
                brief = read_text_brief_file(path)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"path": str(path), "brief": brief_to_form(brief)}

    @app.post("/api/runs", dependencies=[Depends(verify_mutation)])
    def start_run(payload: LaunchPayload) -> dict:
        return app.state.jobs.start(payload).snapshot()

    @app.get("/api/runs/{job_id}")
    def get_run(job_id: str) -> dict:
        try:
            return app.state.jobs.get(job_id).snapshot()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="运行记录不存在") from exc

    @app.post("/api/runs/{job_id}/cancel", dependencies=[Depends(verify_mutation)])
    def cancel_run(job_id: str) -> dict:
        try:
            return app.state.jobs.cancel(job_id).snapshot()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="运行记录不存在") from exc

    @app.post("/api/formal/workspaces/{workspace_id}/cloud-runs")
    def create_cloud_run(
        workspace_id: str,
        payload: CloudRunPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            api = get_agent_api()
            project = api.services.projects.get_project(
                x_mplus_agent_auth,
                payload.project_id,
            )
            if str(project.get("workspace_id") or "") != workspace_id:
                raise WorkspaceServiceError("FORBIDDEN", "项目不属于指定工作区")
            if str(project.get("category_id") or "") != payload.category_id:
                raise WorkspaceServiceError("INVALID_INPUT", "云端运行类别与项目类别不一致")
            with api.services.store.connection() as connection:
                require_permission(
                    connection,
                    x_mplus_agent_auth,
                    workspace_id=workspace_id,
                    project_id=payload.project_id,
                    permission="run.generate",
                )
            request = dict(payload.request or {})
            if set(request) - {
                "max_loops", "quality_profile", "ai_provider", "ai_model",
                "prompt_version", "prompt_schema_version",
            }:
                raise WorkspaceServiceError("INVALID_INPUT", "云端运行包含未允许参数")
            max_loops = request.get("max_loops", 3)
            quality_profile = request.get("quality_profile", "draft")
            if type(max_loops) is not int or not 1 <= max_loops <= 8:
                raise WorkspaceServiceError("INVALID_INPUT", "max_loops 必须为 1-8")
            if quality_profile not in {"draft", "client-review"}:
                raise WorkspaceServiceError("INVALID_INPUT", "quality_profile 无效")
            if payload.runner_version != __version__:
                raise WorkspaceServiceError("INVALID_INPUT", "客户端 runner 版本与服务端不一致")
            run = get_cloud_run_repository().create_run(
                workspace_id=workspace_id,
                project_id=payload.project_id,
                category_id=payload.category_id,
                package_ref=payload.package_ref,
                input_ref=payload.input_ref,
                input_sha256=payload.input_sha256,
                input_filename=payload.input_filename,
                input_size_bytes=payload.input_size_bytes,
                input_files=[item.model_dump() for item in payload.input_files] or None,
                runner_version=__version__,
                cli_version=payload.runner_version,
                idempotency_key=payload.idempotency_key,
                source_commit=configured_source_commit(),
                container_image_digest=os.environ.get("MPLUS_CONTAINER_IMAGE_DIGEST"),
                invocation_source=payload.invocation_source,
                parent_run_id=payload.parent_run_id,
                source_run_id=payload.source_run_id,
                request={**request, "max_loops": max_loops, "quality_profile": quality_profile},
            )
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _cloud_run_dto(run)

    @app.get("/api/formal/workspaces/{workspace_id}/cloud-runs/{run_id}")
    def get_cloud_run(
        workspace_id: str,
        run_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            get_agent_api().services.projects.get_workspace(
                x_mplus_agent_auth,
                workspace_id,
            )
            run = get_cloud_run_repository().get_run(run_id)
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if str(run.get("workspace_id") or "") != workspace_id:
            raise HTTPException(status_code=404, detail="云端运行不存在")
        return _cloud_run_dto(run)

    @app.post("/api/formal/workspaces/{workspace_id}/cloud-runs/{run_id}/cancel")
    def cancel_cloud_run(
        workspace_id: str,
        run_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            api = get_agent_api()
            api.services.identity.validate_session(x_mplus_agent_auth)
            run = get_cloud_run_repository().get_run(run_id)
            if str(run.get("workspace_id") or "") != workspace_id:
                raise RuntimeError("云端运行不存在")
            with api.services.store.connection() as connection:
                require_permission(
                    connection,
                    x_mplus_agent_auth,
                    workspace_id=workspace_id,
                    project_id=run["project_id"],
                    permission="run.cancel",
                )
            return _cloud_run_dto(get_cloud_run_repository().cancel(run_id))
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/formal/workspaces/{workspace_id}/cloud-runs/{run_id}/artifacts")
    def download_cloud_artifact(
        workspace_id: str,
        run_id: str,
        artifact_path: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            get_agent_api().services.projects.get_workspace(
                x_mplus_agent_auth,
                workspace_id,
            )
            run = get_cloud_run_repository().get_run(run_id)
            if str(run.get("workspace_id") or "") != workspace_id:
                raise RuntimeError("云端运行不存在")
            if run.get("artifact_availability") == "metadata_only":
                raise RuntimeError("该运行只有溯源元数据，没有可下载文件")
            if not run.get("result_ref") or app.state.cloud_storage is None:
                raise RuntimeError("云端交付物不可用")
            with app.state.cloud_storage.open(
                run["result_ref"], expected_sha256=run.get("result_sha256")
            ) as stream:
                catalog = json.load(stream)
            normalized = str(artifact_path or "").strip()
            candidates = [
                item for item in list(catalog.get("files") or [])
                if item.get("path") == normalized
            ]
            if (
                len(candidates) != 1
                or not normalized.startswith("output/")
                or Path(normalized).suffix.casefold() not in {".xlsx", ".xlsm", ".pdf"}
            ):
                raise RuntimeError("交付物不存在或不允许下载")
            signed = app.state.cloud_storage.signed_download_url(
                candidates[0]["storage_ref"],
                expires_seconds=600,
            )
            return {
                "url": signed,
                "expires_in": 600,
                "path": normalized,
                "sha256": candidates[0]["sha256"],
                "size_bytes": candidates[0]["size_bytes"],
            }
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc
        except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/history")
    def history() -> dict:
        return {"items": app_history()}

    def _normalized_historical_brief(value: dict) -> dict:
        aliases = {
            "project_name": ("project_name", "项目名称"),
            "brand": ("brand", "client", "品牌或客户"),
            "script_type": ("script_type", "脚本类型", "具体类型"),
            "duration": ("duration", "duration_seconds", "视频时长", "目标时长"),
            "audience": ("audience", "目标受众", "受众"),
            "core_message": ("core_message", "视频目标", "核心信息"),
            "must_include": ("must_include", "required_content", "必须出现的内容"),
            "forbidden_content": (
                "forbidden_content", "禁忌或不能出现的内容", "禁止或不能出现的内容",
            ),
            "notes": ("notes", "note", "备注"),
        }
        safe = {}
        for target, names in aliases.items():
            selected = next(
                (value[name] for name in names if value.get(name) not in (None, "", [])),
                None,
            )
            if selected is not None:
                safe[target] = selected
        return safe

    def _bound_formal_brief(
        workspace_id: str, project_id: str, run_id: str, auth_token: str,
    ) -> dict:
        if formal_brief_adapter is None:
            return {}
        with get_agent_api().services.store.connection() as connection:
            row = connection.execute(
                """SELECT c.brief_version_id
                FROM delivery_snapshots d
                JOIN brief_confirmations c
                  ON c.brief_snapshot_id=d.brief_snapshot_id
                 AND c.workspace_id=d.workspace_id AND c.project_id=d.project_id
                WHERE d.workspace_id=? AND d.project_id=? AND d.run_id=?""",
                (workspace_id, project_id, run_id),
            ).fetchone()
        if row is None:
            return {}
        try:
            employee = formal_brief_adapter.read_structured(
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                brief_version_id=row["brief_version_id"],
            )
        except (FormalBriefDomainError, WorkspaceServiceError):
            return {}
        facts = {}
        for item in (employee.get("brief") or {}).get("facts") or []:
            raw = item.get("value")
            try:
                value = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                value = raw
            facts[str(item.get("label") or "")] = value
        return _normalized_historical_brief(facts)

    def _allowed_historical_artifacts(
        item: dict, project: dict, auth_token: str,
    ) -> list[dict]:
        artifacts = list(item.get("artifacts") or [])
        if formal_review_service is not None:
            try:
                bound = formal_review_service.delivery_snapshots.history_artifacts(
                    auth_token,
                    workspace_id=project["workspace_id"],
                    project_id=project["project_id"],
                    run_id=str(item.get("run_id") or ""),
                )
            except ArtifactResolutionError as exc:
                raise WorkspaceServiceError(exc.code, exc.message) from exc
            for value in bound:
                registered = app.state.artifacts.register(
                    value["path"], value["kind"], value["label"],
                )
                if registered:
                    artifacts.append(registered)
        return list({value["id"]: value for value in artifacts if value.get("id")}.values())

    def _scoped_historical_run(
        workspace_id: str, run_id: str, auth_token: str,
    ) -> tuple[dict, dict]:
        projects = get_agent_api().services.projects
        projects.get_workspace(auth_token, workspace_id)
        matches = [
            item for item in app_history(limit=None, run_id=run_id)
            if str(item.get("run_id") or "") == run_id
        ]
        if len(matches) != 1:
            raise WorkspaceServiceError("NOT_FOUND", "历史运行不存在")
        item = matches[0]
        project_id = str(item.get("project_id") or "")
        if not project_id:
            raise WorkspaceServiceError(
                "HISTORICAL_RUN_SCOPE_UNRESOLVED", "该归档没有可核验项目身份，不能在 Formal 展开",
            )
        project = projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "历史运行不属于当前 workspace")
        item = dict(item)
        item["brief"] = (
            _normalized_historical_brief(dict(item.get("brief") or {}))
            or _bound_formal_brief(workspace_id, project_id, run_id, auth_token)
        )
        return item, project

    def _historical_review_import_access(
        item: dict, project: dict, auth_token: str,
    ) -> tuple[bool, str, str]:
        if bool(item.get("read_only_legacy")):
            return False, "该历史记录是只读兼容记录，不能导入复核结果。", "REVIEW_IMPORT_NOT_AVAILABLE"
        if formal_review_service is None:
            return False, "当前没有可用的正式审核服务，不能导入复核结果。", "REVIEW_IMPORT_NOT_AVAILABLE"
        version_id = str(project.get("current_version_id") or "")
        if not version_id:
            return False, "当前项目还没有可审核的正式版本。", "REVIEW_IMPORT_NOT_AVAILABLE"
        try:
            context = formal_review_service.services.delivery.resolve(
                auth_token,
                project["project_id"],
                version_id,
                expected_project_version=project["project_version"],
            )
            if context.run_id != str(item.get("run_id") or ""):
                return (
                    False,
                    "该历史运行不是当前正式版本；请先创建受控子版本再导入复核结果。",
                    "HISTORICAL_VERSION_NOT_CURRENT",
                )
            review = formal_review_service.read_current(
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                expected_project_version=project["project_version"],
            )
        except (WorkspaceServiceError, ArtifactResolutionError):
            return False, "当前历史版本尚未具备可导入的正式审核上下文。", "REVIEW_IMPORT_NOT_AVAILABLE"
        state = str(review.get("state") or "")
        if state == "confirmed":
            return False, "当前历史版本已完成整片确认，无需重复导入复核结果。", "FINAL_CONFIRMATION_EXISTS"
        if state in {"ready", "reviewing", "ready_final"}:
            return True, "", ""
        return False, "当前历史版本尚未进入可导入人工复核结果的审核状态。", "REVIEW_IMPORT_NOT_AVAILABLE"

    @app.get("/api/formal/workspaces/{workspace_id}/legacy-runs")
    def formal_legacy_runs(
        workspace_id: str,
        project_id: str = "",
        status: str = "",
        limit: int = 300,
        page_size: int | None = None,
        cursor: str = "",
        query: str = "",
        created_from: str = "",
        created_to: str = "",
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        projects = get_agent_api().services.projects
        try:
            projects.get_workspace(x_mplus_agent_auth, workspace_id)
            if project_id:
                selected = projects.get_project(x_mplus_agent_auth, project_id)
                if selected["workspace_id"] != workspace_id:
                    raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "历史项目不属于当前 workspace")
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc
        scope = [workspace_id, project_id, status, query, created_from, created_to]
        boundary = None
        try:
            if len(cursor) > 4096:
                raise ValueError("cursor too long")
            if cursor:
                saved = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if saved['scope'] != scope or not isinstance(saved['run_id'], str):
                    raise ValueError("cursor scope changed")
                boundary = (float(saved['updated_at']), saved['run_id'])
                if not math.isfinite(boundary[0]):
                    raise ValueError('invalid cursor timestamp')
            def parse_date(value):
                parsed = datetime.fromisoformat(value)
                return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
            lower = parse_date(created_from).timestamp() if created_from else None
            upper = parse_date(created_to) if created_to else None
            if upper and len(created_to) == 10:
                upper += timedelta(days=1)
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise HTTPException(status_code=422, detail={"code": "INVALID_INPUT", "message": "历史记录筛选或翻页游标无效，请返回第一页。"}) from exc
        items = []
        authorized_projects = {}
        for item in app_history(limit=None):
            explicit_project_id = str(item.get("project_id") or "")
            if not explicit_project_id:
                continue
            if project_id and explicit_project_id != project_id:
                continue
            try:
                if explicit_project_id not in authorized_projects:
                    authorized_projects[explicit_project_id] = projects.get_project(x_mplus_agent_auth, explicit_project_id)
                project = authorized_projects[explicit_project_id]
                if project is None:
                    continue
            except WorkspaceServiceError as exc:
                if exc.code in {"NOT_FOUND", "PERMISSION_DENIED"}:
                    authorized_projects[explicit_project_id] = None
                    continue
                raise agent_error(exc) from exc
            if project["workspace_id"] != workspace_id:
                continue
            if project_id and explicit_project_id != project_id:
                continue
            if status and str(item.get("state") or "") != status:
                continue
            updated_at = float(item.get("updated_at") or 0)
            if lower is not None and updated_at < lower or upper is not None and updated_at >= upper.timestamp():
                continue
            if query.strip().casefold() not in " ".join(str(value or "") for value in (
                    item.get("run_id"), project.get("name"), item.get("message"))).casefold():
                continue
            items.append({
                "run_id": item["run_id"],
                "parent_run_id": item["parent_run_id"],
                "project_id": explicit_project_id or None,
                "project_name": str(project.get("name") or "当前项目"),
                "state": item["state"],
                "message": item["message"],
                "updated_at": item["updated_at"],
                "artifact_availability": item.get("artifact_availability", "metadata_only"),
            })
        items.sort(key=lambda item: (float(item['updated_at']), item['run_id']), reverse=True)
        total = len(items)
        if boundary is not None:
            items = [item for item in items if (float(item['updated_at']), item['run_id']) < boundary]
        size = max(1, min(page_size if page_size is not None else limit, 500))
        page = items[:size]
        next_cursor = None
        if len(items) > size:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps({"scope": scope,
                "updated_at": last['updated_at'], "run_id": last['run_id']}).encode()).decode()
        return {"items": page, "count": len(page), "matched_total": total,
                "next_cursor": next_cursor, "sort_field": "updated_at"}

    @app.get("/api/formal/workspaces/{workspace_id}/legacy-runs/{run_id}")
    def formal_legacy_run_detail(
        workspace_id: str,
        run_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            item, project = _scoped_historical_run(
                workspace_id, run_id, x_mplus_agent_auth,
            )
            project_id = project["project_id"]
            brief = dict(item.get("brief") or {})
            safe_brief_keys = {
                "project_name", "brand", "script_type", "duration", "audience",
                "core_message", "must_include", "forbidden_content", "notes",
            }
            safe_brief = {key: brief[key] for key in safe_brief_keys if brief.get(key) not in (None, "", [])}
            artifacts = [
                {
                    "artifact_id": str(value.get("id") or ""),
                    "kind": str(value.get("kind") or ""),
                    "label": str(value.get("label") or "允许访问的产物"),
                    "filename": Path(str(value.get("filename") or "产物")).name,
                    "download_url": (
                        f"/api/formal/workspaces/{workspace_id}/legacy-runs/{run_id}"
                        f"/artifacts/{value.get('id')}"
                    ),
                }
                for value in _allowed_historical_artifacts(
                    item, project, x_mplus_agent_auth,
                ) if value.get("id")
            ]
            review_import, review_import_reason, _review_import_code = (
                _historical_review_import_access(item, project, x_mplus_agent_auth)
            )
            return {
                "run_id": run_id,
                "project_id": project_id,
                "project_name": str(project.get("name") or "当前项目"),
                "state": str(item.get("state") or ""),
                "message": str(item.get("message") or ""),
                "brief": safe_brief,
                "artifacts": artifacts,
                "actions": {
                    "review_import": review_import,
                    "review_import_reason": review_import_reason,
                    "version_feedback": True,
                    "continue_from_brief": bool(safe_brief),
                },
                "production_certification": "BLOCK",
            }
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.post("/api/formal/workspaces/{workspace_id}/legacy-runs/{run_id}/continuation-proposals")
    def propose_historical_continuation(
        workspace_id: str,
        run_id: str,
        payload: HistoricalContinuePayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            if formal_brief_adapter is None or formal_brief_upload_factory is None:
                raise WorkspaceServiceError("FORMAL_BRIEF_UNAVAILABLE", "Formal Brief 权威服务未安装")
            item, project = _scoped_historical_run(
                workspace_id, run_id, x_mplus_agent_auth,
            )
            brief = dict(item.get("brief") or {})
            safe_keys = {
                "project_name", "brand", "script_type", "duration", "audience",
                "core_message", "must_include", "forbidden_content", "notes",
            }
            safe_brief = {
                key: brief[key] for key in safe_keys if brief.get(key) not in (None, "", [])
            }
            if not safe_brief:
                raise WorkspaceServiceError("HISTORICAL_BRIEF_UNAVAILABLE", "历史运行没有允许续作的 Brief")
            parameters = {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "source_project_id": project["project_id"],
                "source_project_version": project["project_version"],
                "category_id": project["category_id"],
                "action": payload.action,
                "safe_brief": safe_brief,
                "idempotency_key": payload.idempotency_key,
            }
            digest = sha256_json({"action": "continue_historical_brief", "parameters": parameters})
            tasks = get_agent_api().services.tasks
            for existing in tasks.list_workspace_tasks(
                x_mplus_agent_auth, workspace_id, project_id=project["project_id"],
            ):
                if (
                    existing.get("task_type") != "formal_historical_continuation"
                    or existing.get("payload_sha256") != digest
                ):
                    continue
                existing_events = tasks.list_task_events(
                    x_mplus_agent_auth, existing["task_id"],
                )
                existing_response = next((
                    event["details"].get("response") for event in reversed(existing_events)
                    if event["details"].get("response", {}).get("proposal_digest") == digest
                ), None)
                if existing_response and existing.get("status") != "failed":
                    return existing_response
                raise WorkspaceServiceError(
                    "IDEMPOTENCY_RETRY_REQUIRED",
                    "上一次历史续作失败；请刷新后使用新的重试请求",
                )
            session = tasks.create_agent_session(
                x_mplus_agent_auth, project_id=project["project_id"],
            )
            task = tasks.create_task(
                x_mplus_agent_auth,
                agent_session_id=session["agent_session_id"],
                task_type="formal_historical_continuation",
                payload_sha256=digest,
                expected_project_version=project["project_version"],
            )
            task = tasks.transition_task(
                x_mplus_agent_auth, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="understanding",
                details={"formal_context_version": project["project_version"]},
            )
            response = {
                "type": "historical_continuation_proposal",
                "proposal_id": task["task_id"],
                "proposal_digest": digest,
                "before": {"project_name": project["name"], "historical_run": run_id[:8]},
                "after": {
                    "target": "当前项目的新 Brief 子版本" if payload.action == "current_project" else "新的受控项目",
                    "brief_field_count": len(safe_brief),
                },
                "affected_fields": sorted(safe_brief),
                "rerun_nodes": ["brief_confirmation", "script_plan"],
                "confirmation_required": True,
                "production_certification": "BLOCK",
            }
            tasks.transition_task(
                x_mplus_agent_auth, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="proposed",
                details={"response": response, "internal_formal_parameters": parameters},
            )
            return response
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.post("/api/formal/workspaces/{workspace_id}/legacy-runs/{run_id}/continuation-proposals/{proposal_id}/confirm")
    def confirm_historical_continuation(
        workspace_id: str,
        run_id: str,
        proposal_id: str,
        payload: HistoricalConfirmPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            if formal_brief_adapter is None or formal_brief_upload_factory is None:
                raise WorkspaceServiceError("FORMAL_BRIEF_UNAVAILABLE", "Formal Brief 权威服务未安装")
            _scoped_historical_run(workspace_id, run_id, x_mplus_agent_auth)
            tasks = get_agent_api().services.tasks
            task = tasks.get_task(x_mplus_agent_auth, proposal_id)
            events = tasks.list_task_events(x_mplus_agent_auth, proposal_id)
            details = next((
                event["details"] for event in reversed(events)
                if event["details"].get("internal_formal_parameters")
            ), None)
            if details is None:
                raise WorkspaceServiceError("NOT_FOUND", "历史续作提案不存在")
            parameters = details["internal_formal_parameters"]
            expected = sha256_json({"action": "continue_historical_brief", "parameters": parameters})
            if expected != payload.proposal_digest:
                raise WorkspaceServiceError("PROPOSAL_DIGEST_MISMATCH", "历史续作提案摘要不匹配")
            if parameters["workspace_id"] != workspace_id or parameters["run_id"] != run_id:
                raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "历史续作提案不属于当前运行")
            if task["status"] == "completed":
                completed = next((
                    event["details"].get("response") for event in reversed(events)
                    if event["details"].get("response", {}).get("type") == "historical_continuation_created"
                ), None)
                if completed:
                    return completed
            projects = get_agent_api().services.projects
            source = projects.get_project(
                x_mplus_agent_auth, parameters["source_project_id"],
            )
            if (
                task["status"] != "executing"
                and source["project_version"] != parameters["source_project_version"]
            ):
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "源项目在提案后已变化")
            if task["status"] == "proposed":
                current = tasks.transition_task(
                    x_mplus_agent_auth, task_id=proposal_id,
                    expected_task_version=task["task_version"], status="waiting_confirmation",
                    details={"proposal_digest": payload.proposal_digest},
                )
            else:
                current = task
            if current["status"] == "waiting_confirmation":
                current = tasks.transition_task(
                    x_mplus_agent_auth, task_id=proposal_id,
                    expected_task_version=current["task_version"], status="executing",
                    details={"proposal_digest": payload.proposal_digest},
                )
            if current["status"] != "executing":
                raise WorkspaceServiceError(
                    "CONTINUATION_RETRY_REQUIRED", "历史续作任务当前不能继续执行",
                )
            target = source
            try:
                if parameters["action"] == "new_project":
                    workspace = projects.get_workspace(x_mplus_agent_auth, workspace_id)
                    name = str(parameters["safe_brief"].get("project_name") or source["name"])
                    target_id = "project_history_" + sha256_json({
                        "workspace_id": workspace_id,
                        "run_id": run_id,
                        "source_project_id": source["project_id"],
                    })[:24]
                    target = projects.create_project(
                        x_mplus_agent_auth,
                        workspace_id=workspace_id,
                        name=f"{name}（历史续作）",
                        category_id=parameters["category_id"],
                        expected_workspace_version=workspace["workspace_version"],
                        _project_id=target_id,
                    )
                text = "\n".join(
                    f"{key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value}"
                    for key, value in parameters["safe_brief"].items()
                ).encode("utf-8")
                imported = formal_brief_adapter.import_uploads(
                    x_mplus_agent_auth,
                    workspace_id=workspace_id,
                    project_id=target["project_id"],
                    uploads=[formal_brief_upload_factory("历史 Brief.txt", BytesIO(text), True)],
                    expected_project_version=target["project_version"],
                )
            except (FormalBriefDomainError, WorkspaceServiceError) as exc:
                latest = tasks.get_task(x_mplus_agent_auth, proposal_id)
                if latest["status"] == "executing":
                    tasks.transition_task(
                        x_mplus_agent_auth, task_id=proposal_id,
                        expected_task_version=latest["task_version"], status="failed",
                        details={"error_code": str(getattr(exc, "code", "CONTINUATION_FAILED"))},
                        error_code=str(getattr(exc, "code", "CONTINUATION_FAILED")),
                    )
                raise
            response = {
                "type": "historical_continuation_created",
                "status": "completed",
                "target_project_id": target["project_id"],
                "target": "current_project" if target["project_id"] == source["project_id"] else "new_project",
                "brief_confirmation_required": True,
                "next_href": f"/projects/{target['project_id']}/brief",
                "production_certification": "BLOCK",
            }
            tasks.transition_task(
                x_mplus_agent_auth, task_id=proposal_id,
                expected_task_version=current["task_version"], status="completed",
                details={"response": response, "brief_key": imported.get("brief_key", "")},
            )
            return response
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise agent_error(exc) from exc

    @app.post("/api/formal/workspaces/{workspace_id}/legacy-runs/{run_id}/review-import-proposals")
    async def propose_historical_review_import(
        workspace_id: str,
        run_id: str,
        request: Request,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            if formal_review_service is None:
                raise WorkspaceServiceError("FORMAL_REVIEW_UNAVAILABLE", "Formal 审核服务未安装")
            item, project = _scoped_historical_run(workspace_id, run_id, x_mplus_agent_auth)
            review_import, review_import_reason, review_import_code = (
                _historical_review_import_access(item, project, x_mplus_agent_auth)
            )
            if not review_import:
                raise WorkspaceServiceError(review_import_code, review_import_reason)
            form = await request.form()
            if set(form.keys()) != {"file"}:
                raise WorkspaceServiceError("INVALID_INPUT", "复核导入只接受一个 file 字段")
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read") or not str(upload.filename or "").endswith(".xlsx"):
                raise WorkspaceServiceError("INVALID_INPUT", "请选择已填写的 .xlsx 人工复核表")
            raw = await upload.read()
            if not raw or len(raw) > 50 * 1024 * 1024:
                raise WorkspaceServiceError("FILE_TOO_LARGE", "复核表为空或超过 50MB")
            context = formal_review_service.services.delivery.resolve(
                x_mplus_agent_auth,
                project["project_id"],
                project["current_version_id"],
                expected_project_version=project["project_version"],
            )
            if context.run_id != run_id:
                raise WorkspaceServiceError(
                    "HISTORICAL_VERSION_NOT_CURRENT",
                    "该历史运行不是当前项目版本；请先从历史版本创建受控子版本再导入复核",
                )
            digest = hashlib.sha256(raw).hexdigest()
            runtime = load_runtime_config()
            evidence_root = Path(runtime["data_root"]) / "formal-history-review-imports"
            evidence_root.mkdir(parents=True, exist_ok=True)
            evidence_path = evidence_root / f"{digest}.xlsx"
            if not evidence_path.exists():
                staged = evidence_root / f".{digest}.{secrets.token_hex(6)}.tmp"
                staged.write_bytes(raw)
                os.replace(staged, evidence_path)
            run_dir = find_history_run_dir(run_id)
            validate_review_workbook(run_dir.parent.parent, evidence_path, run_id=run_id)
            report = analyze_review_workbooks([evidence_path])
            if report["status"] != "complete":
                raise WorkspaceServiceError("REVIEW_IMPORT_INCOMPLETE", "复核表仍有未完成或无效审核项")
            parameters = {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "project_id": project["project_id"],
                "expected_project_version": project["project_version"],
                "evidence_path": str(evidence_path),
                "evidence_sha256": digest,
            }
            proposal_digest = sha256_json({"action": "import_formal_historical_review", "parameters": parameters})
            tasks = get_agent_api().services.tasks
            session = tasks.create_agent_session(
                x_mplus_agent_auth, project_id=project["project_id"],
            )
            task = tasks.create_task(
                x_mplus_agent_auth,
                agent_session_id=session["agent_session_id"],
                task_type="formal_historical_review_import",
                payload_sha256=proposal_digest,
                expected_project_version=project["project_version"],
            )
            task = tasks.transition_task(
                x_mplus_agent_auth, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="understanding",
                details={"formal_context_version": project["project_version"]},
            )
            project_report = report["projects"][0]
            response = {
                "type": "historical_review_import_proposal",
                "proposal_id": task["task_id"],
                "proposal_digest": proposal_digest,
                "before": {"review_state": "current_formal_version"},
                "after": {
                    "human_review_status": project_report["human_review_status"],
                    "issue_count": len(project_report.get("issues") or []),
                },
                "affected_fields": ["manual_review_decisions"],
                "rerun_nodes": ["human_review_gate"],
                "confirmation_required": True,
                "production_certification": "BLOCK",
            }
            tasks.transition_task(
                x_mplus_agent_auth, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="proposed",
                details={"response": response, "internal_formal_parameters": parameters},
            )
            return response
        except (ReviewPackageError, ReviewWorkbookError, VisualCandidateError, OSError, json.JSONDecodeError) as exc:
            raise agent_error(WorkspaceServiceError("REVIEW_IMPORT_INVALID", str(exc))) from exc
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.post("/api/formal/workspaces/{workspace_id}/legacy-runs/{run_id}/review-import-proposals/{proposal_id}/confirm")
    def confirm_historical_review_import(
        workspace_id: str,
        run_id: str,
        proposal_id: str,
        payload: HistoricalConfirmPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            if formal_review_service is None:
                raise WorkspaceServiceError("FORMAL_REVIEW_UNAVAILABLE", "Formal 审核服务未安装")
            item, project = _scoped_historical_run(workspace_id, run_id, x_mplus_agent_auth)
            tasks = get_agent_api().services.tasks
            task = tasks.get_task(x_mplus_agent_auth, proposal_id)
            events = tasks.list_task_events(x_mplus_agent_auth, proposal_id)
            details = next((
                event["details"] for event in reversed(events)
                if event["details"].get("internal_formal_parameters")
            ), None)
            if details is None:
                raise WorkspaceServiceError("NOT_FOUND", "复核导入提案不存在")
            parameters = details["internal_formal_parameters"]
            expected = sha256_json({"action": "import_formal_historical_review", "parameters": parameters})
            if expected != payload.proposal_digest:
                raise WorkspaceServiceError("PROPOSAL_DIGEST_MISMATCH", "复核导入提案摘要不匹配")
            if parameters["workspace_id"] != workspace_id or parameters["run_id"] != run_id:
                raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "复核导入提案不属于当前历史运行")
            completed_response = next((
                event["details"].get("response") for event in reversed(events)
                if event["details"].get("response", {}).get("type") == "historical_review_imported"
            ), None)
            if task["status"] == "completed" and completed_response:
                return completed_response
            if task["status"] in {"failed", "cancelled"}:
                raise WorkspaceServiceError("HISTORICAL_REVIEW_TASK_TERMINAL", "复核导入任务已终止，不能继续执行")
            review_import, review_import_reason, review_import_code = (
                _historical_review_import_access(item, project, x_mplus_agent_auth)
            )
            if not review_import:
                if task["status"] == "blocked" and review_import_code == "REVIEW_IMPORT_NOT_AVAILABLE":
                    # A return decision legitimately makes read_current() report
                    # needs_change. The blocked task is the recovery authority.
                    pass
                elif not (
                    review_import_code == "FINAL_CONFIRMATION_EXISTS"
                    and task["status"] == "blocked"
                ):
                    raise WorkspaceServiceError(review_import_code, review_import_reason)
                else:
                    current = formal_review_service.read_current(
                        x_mplus_agent_auth,
                        workspace_id=workspace_id,
                        project_id=project["project_id"],
                        expected_project_version=project["project_version"],
                    )
                    if current.get("state") != "confirmed":
                        raise WorkspaceServiceError(review_import_code, review_import_reason)
                    resumed = tasks.transition_task(
                        x_mplus_agent_auth,
                        task_id=proposal_id,
                        expected_task_version=task["task_version"],
                        status="executing",
                        details={
                            "proposal_digest": payload.proposal_digest,
                            "resume_from": "authoritative_review_read_model",
                        },
                    )
                    response = {
                        "type": "historical_review_imported",
                        "status": "completed",
                        "review_state": current["state"],
                        "returned_shot_count": int(
                            next((
                                event["details"].get("returned_shot_count", 0)
                                for event in reversed(events)
                                if event["details"].get("saga_step")
                            ), 0)
                        ),
                        "production_certification": "BLOCK",
                    }
                    tasks.transition_task(
                        x_mplus_agent_auth,
                        task_id=proposal_id,
                        expected_task_version=resumed["task_version"],
                        status="completed",
                        details={"response": response},
                    )
                    return response
            evidence_path = Path(parameters["evidence_path"])
            if not evidence_path.is_file() or sha256_file(evidence_path) != parameters["evidence_sha256"]:
                raise WorkspaceServiceError("REVIEW_IMPORT_EVIDENCE_DRIFT", "复核导入证据已变化")
            current_project = get_agent_api().services.projects.get_project(
                x_mplus_agent_auth, project["project_id"],
            )
            if current_project["project_version"] != parameters["expected_project_version"]:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目在复核确认前已变化")
            run_dir = find_history_run_dir(run_id)
            validate_review_workbook(run_dir.parent.parent, evidence_path, run_id=run_id)
            report = analyze_review_workbooks([evidence_path])
            if report["status"] != "complete":
                raise WorkspaceServiceError("REVIEW_IMPORT_INCOMPLETE", "复核表在确认前变为不完整")
            try:
                return _run_historical_review_import_saga(
                    tasks=tasks,
                    formal_review_service=formal_review_service,
                    auth_token=x_mplus_agent_auth,
                    task=task,
                    proposal_id=proposal_id,
                    proposal_digest=payload.proposal_digest,
                    workspace_id=workspace_id,
                    project_id=project["project_id"],
                    expected_project_version=current_project["project_version"],
                    report=report,
                )
            except Exception as exc:
                if isinstance(exc, WorkspaceServiceError):
                    raise agent_error(exc) from exc
                raise agent_error(WorkspaceServiceError(
                    "HISTORICAL_REVIEW_IMPORT_FAILED",
                    "历史复核导入未完成，任务已阻断；请使用相同提案重试",
                )) from exc
        except (ReviewPackageError, ReviewWorkbookError, VisualCandidateError, OSError, json.JSONDecodeError) as exc:
            raise agent_error(WorkspaceServiceError("REVIEW_IMPORT_INVALID", str(exc))) from exc
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.get("/api/formal/workspaces/{workspace_id}/legacy-runs/{run_id}/artifacts/{artifact_id}")
    def formal_legacy_artifact(
        workspace_id: str,
        run_id: str,
        artifact_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> FileResponse:
        try:
            item, _project = _scoped_historical_run(
                workspace_id, run_id, x_mplus_agent_auth,
            )
            allowed = {
                str(value.get("id") or "")
                for value in _allowed_historical_artifacts(
                    item, _project, x_mplus_agent_auth,
                ) if value.get("id")
            }
            if artifact_id not in allowed:
                raise WorkspaceServiceError("NOT_FOUND", "产物不存在或无权访问")
            artifact = app.state.artifacts.get(artifact_id)
            path = app.state.artifacts.materialize(artifact_id)
            return FileResponse(path, filename=artifact.filename)
        except KeyError as exc:
            raise agent_error(WorkspaceServiceError("NOT_FOUND", "产物不存在或已失效")) from exc
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.get("/api/formal/workspaces/{workspace_id}/system")
    def formal_system_status(
        workspace_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            get_agent_api().services.projects.get_workspace(
                x_mplus_agent_auth, workspace_id
            )
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc
        checks = []
        for check in system_checks():
            name = str(check["name"])
            message = str(check["message"])
            if name.endswith("资料库"):
                message = "已连接" if check["status"] == "pass" else "尚未配置可用资料"
            elif name == "输出根目录":
                message = "存储可用" if check["status"] == "pass" else "存储不可用"
            checks.append({"name": name, "status": check["status"], "message": message})
        service = app.state.formal_admin_service
        if service is not None:
            try:
                verified = service.connection_status(x_mplus_agent_auth, workspace_id=workspace_id)
            except WorkspaceServiceError as exc:
                if exc.code != "PERMISSION_DENIED":
                    raise agent_error(exc) from exc
            else:
                by_kind = {item["kind"]: item for item in verified["services"]}
                for check in checks:
                    kind = {"文案生成": "text", "视觉 AI": "vision"}.get(check["name"])
                    if kind:
                        item = by_kind[kind]
                        receipt = item["verification"]
                        check["status"] = {"succeeded": "pass", "failed": "fail", "unconfigured": "fail"}.get(receipt["status"], "unverified")
                        check["message"] = ("已配置。" if item["configured"] else "尚未配置。") + receipt["summary"]
        attention_count = sum(check["status"] == "fail" for check in checks)
        unverified_count = sum(check["status"] == "unverified" for check in checks)
        return {
            "overall_status": "needs_attention" if attention_count else "unverified" if unverified_count else "ready",
            "summary": (
                f"有 {attention_count} 项需要处理" if attention_count
                else f"配置已读取，{unverified_count} 项尚未验证" if unverified_count
                else "本机运行依赖可用"
            ),
            "attention_count": attention_count,
            "checks": checks,
        }

    def require_formal_admin() -> FormalAdminService:
        service = app.state.formal_admin_service
        if service is None:
            raise WorkspaceServiceError(
                "FORMAL_ADMIN_UNAVAILABLE", "管理员诊断与受控恢复服务尚未接入",
            )
        return service

    @app.get("/api/formal/admin/workspaces/{workspace_id}/access")
    def formal_admin_access(
        workspace_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return require_formal_admin().access(
                x_mplus_agent_auth, workspace_id=workspace_id,
            )
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.get("/api/formal/admin/workspaces/{workspace_id}/approvals")
    def formal_admin_approvals(workspace_id: str, x_mplus_agent_auth: str = Header(default="")) -> dict:
        try:
            return require_formal_admin().approval_summary(x_mplus_agent_auth, workspace_id=workspace_id,
                workbench=get_ui_workbench())
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.get("/api/formal/admin/workspaces/{workspace_id}/connections")
    def formal_admin_connections(workspace_id: str, x_mplus_agent_auth: str = Header(default="")) -> dict:
        try:
            return require_formal_admin().connection_status(x_mplus_agent_auth, workspace_id=workspace_id)
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.post("/api/formal/admin/workspaces/{workspace_id}/connections", dependencies=[Depends(verify_mutation)])
    def formal_admin_check_connection(workspace_id: str, payload: AdminConnectionCheckPayload,
                                      x_mplus_agent_auth: str = Header(default="")) -> dict:
        try:
            return require_formal_admin().test_connection(x_mplus_agent_auth, workspace_id=workspace_id,
                kind=payload.kind, request_id=payload.request_id)
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.get("/api/formal/admin/workspaces/{workspace_id}/overview")
    def formal_admin_overview(
        workspace_id: str,
        limit: int = 100,
        page_size: Optional[int] = None,
        cursor: Optional[str] = None,
        project_id: Optional[str] = None,
        status: Optional[str] = None,
        query: str = "",
        created_from: str = "",
        created_to: str = "",
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return require_formal_admin().overview(
                x_mplus_agent_auth, workspace_id=workspace_id, limit=limit,
                page_size=page_size, cursor=cursor, project_id=project_id, status=status,
                query=query, created_from=created_from, created_to=created_to,
            )
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.get("/api/formal/admin/workspaces/{workspace_id}/tasks/{task_id}")
    def formal_admin_task_detail(
        workspace_id: str,
        task_id: str,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return require_formal_admin().task_detail(
                x_mplus_agent_auth, workspace_id=workspace_id, task_id=task_id,
            )
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.post("/api/formal/admin/workspaces/{workspace_id}/tasks/{task_id}/recovery-proposals")
    def formal_admin_recovery_proposal(
        workspace_id: str,
        task_id: str,
        payload: AdminRecoveryProposalPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return require_formal_admin().propose_recovery(
                x_mplus_agent_auth,
                workspace_id=workspace_id,
                task_id=task_id,
                **payload.model_dump(),
            )
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.post("/api/formal/admin/workspaces/{workspace_id}/recovery-proposals/{proposal_id}/confirm")
    def formal_admin_recovery_confirm(
        workspace_id: str,
        proposal_id: str,
        payload: AdminRecoveryConfirmPayload,
        x_mplus_agent_auth: str = Header(default=""),
    ) -> dict:
        try:
            return require_formal_admin().confirm_recovery(
                x_mplus_agent_auth,
                workspace_id=workspace_id,
                proposal_id=proposal_id,
                proposal_digest=payload.proposal_digest,
            )
        except WorkspaceServiceError as exc:
            raise agent_error(exc) from exc

    @app.post("/api/dialog/{kind}", dependencies=[Depends(verify_mutation)])
    def dialog(kind: Literal["brief", "image", "review", "asset", "media", "source", "output", "package"]) -> dict:
        try:
            path = choose_local_path(kind)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"cancelled": path is None, "path": path or ""}

    @app.post("/api/runs/{job_id}/feedback", dependencies=[Depends(verify_mutation)])
    def submit_feedback(job_id: str, payload: FeedbackPayload) -> dict:
        try:
            parent = app.state.jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="运行记录不存在") from exc
        if parent.state not in TERMINAL_STATES:
            raise HTTPException(status_code=409, detail="当前运行尚未结束，暂不能创建修改版本")
        if payload.category == "image" and not payload.shot_no.strip():
            raise HTTPException(status_code=422, detail="图片问题必须填写镜号，程序才能只重配对应镜头")
        manifest_path = parent.manifest_path
        if manifest_path is None and parent.report:
            manifest_path = Path(str(parent.report.get("run_manifest_path") or ""))
        if not manifest_path or not manifest_path.is_file():
            raise HTTPException(status_code=409, detail="找不到父版本 run_manifest，无法建立版本链")
        return _start_feedback_revision(
            app.state.jobs,
            manifest_path,
            payload,
            parent.payload,
            prior_constraints_path=parent.payload.options.revision_constraints or None,
        )

    @app.post("/api/history/{run_id}/feedback", dependencies=[Depends(verify_mutation)])
    def submit_history_feedback(run_id: str, payload: FeedbackPayload) -> dict:
        try:
            run_dir = find_history_run_dir(run_id)
            parent_payload = launch_payload_from_run(run_dir)
            return _start_feedback_revision(
                app.state.jobs,
                run_dir / "run_manifest.json",
                payload,
                parent_payload,
                prior_constraints=run_constraints(run_dir),
            )
        except (VisualCandidateError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/history/{run_id}/review", dependencies=[Depends(verify_mutation)])
    def import_history_review(run_id: str, payload: PathPayload) -> dict:
        review = Path(payload.path).expanduser().resolve()
        if not review.is_file() or review.suffix.casefold() != ".xlsx":
            raise HTTPException(status_code=422, detail="请选择已填写的 .xlsx 人工复核表")
        try:
            run_dir = find_history_run_dir(run_id)
            parent_payload = launch_payload_from_run(run_dir)
            options = parent_payload.options.model_copy(update={
                "resume_run_id": run_id,
                "from_stage": "",
                "retry_stage": "",
                "review_workbook": str(review),
                "allow_cached_analysis": True,
            })
            return app.state.jobs.start(
                parent_payload.model_copy(update={"options": options})
            ).snapshot()
        except (VisualCandidateError, ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/runs/{job_id}/visual-candidates", dependencies=[Depends(verify_mutation)])
    def visual_candidates(job_id: str, payload: CandidateQuery) -> dict:
        try:
            job = app.state.jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="运行记录不存在") from exc
        manifest_path = job.manifest_path
        if manifest_path is None and job.report:
            manifest_path = Path(str(job.report.get("run_manifest_path") or ""))
        if not manifest_path or not manifest_path.is_file():
            raise HTTPException(status_code=409, detail="找不到运行版本，不能检索图片")
        try:
            result = candidates_for_run(
                manifest_path.parent,
                shot_no=payload.shot_no,
                query=payload.query,
            )
        except (VisualCandidateError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _register_candidate_previews(app.state.artifacts, result, payload.shot_no)
        return result

    @app.post("/api/history/{run_id}/visual-candidates", dependencies=[Depends(verify_mutation)])
    def history_visual_candidates(run_id: str, payload: CandidateQuery) -> dict:
        try:
            result = candidates_for_run(
                find_history_run_dir(run_id),
                shot_no=payload.shot_no,
                query=payload.query,
            )
        except (VisualCandidateError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _register_candidate_previews(app.state.artifacts, result, payload.shot_no)
        return result

    def artifact_or_404(artifact_id: str) -> Artifact:
        try:
            return app.state.artifacts.get(artifact_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="产物不存在或已失效") from exc

    @app.get("/api/artifacts/{artifact_id}", dependencies=[Depends(verify_mutation)])
    def download_artifact(artifact_id: str) -> FileResponse:
        artifact = artifact_or_404(artifact_id)
        try:
            path = app.state.artifacts.materialize(artifact_id)
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="云端产物暂时无法读取") from exc
        return FileResponse(path, filename=artifact.filename)

    @app.get("/api/previews/{artifact_id}")
    def preview_artifact(artifact_id: str, token: str = "") -> FileResponse:
        if not secrets.compare_digest(token, app.state.session_token):
            raise HTTPException(status_code=403, detail="本地会话已失效")
        artifact = artifact_or_404(artifact_id)
        if artifact.kind != "candidate_image":
            raise HTTPException(status_code=404, detail="预览不存在")
        return FileResponse(app.state.artifacts.materialize(artifact_id))

    @app.post("/api/artifacts/{artifact_id}/open", dependencies=[Depends(verify_mutation)])
    def open_artifact(artifact_id: str) -> dict:
        artifact_or_404(artifact_id)
        path = app.state.artifacts.materialize(artifact_id)
        subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"opened": True}

    @app.post("/api/artifacts/{artifact_id}/reveal", dependencies=[Depends(verify_mutation)])
    def reveal_artifact(artifact_id: str) -> dict:
        artifact_or_404(artifact_id)
        path = app.state.artifacts.materialize(artifact_id)
        subprocess.Popen(["open", "-R", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"opened": True}

    app.mount("/assets", StaticFiles(directory=UI_ROOT), name="assets")

    if agent_api is not None:
        install_formal_routes(
            app,
            agent_api=agent_api,
            brief_adapter=formal_brief_adapter,
            brief_upload_factory=formal_brief_upload_factory,
            formal_generation_service=formal_generation_service,
            formal_script_service=formal_script_service,
            formal_image_service=formal_image_service,
            formal_review_service=formal_review_service,
            formal_admin_service=formal_admin_service,
        )

    def ui_build_id() -> str:
        digest = hashlib.sha256()
        for name in ("index.html", "app.js", "app.css", "image-creation.js"):
            digest.update((UI_ROOT / name).read_bytes())
        return digest.hexdigest()[:20]

    def product_index() -> Response:
        content = (UI_ROOT / "index.html").read_text(encoding="utf-8")
        return Response(
            content=content.replace("__MPLUS_UI_BUILD__", ui_build_id()),
            media_type="text/html", headers={"Cache-Control": "no-cache"},
        )

    @app.get("/projects")
    @app.get("/projects/new")
    @app.get("/tasks")
    @app.get("/materials")
    @app.get("/shot-images")
    @app.get("/tools")
    @app.get("/assistant")
    @app.get("/assets")
    @app.get("/knowledge")
    @app.get("/system")
    @app.get("/admin")
    def product_dashboard() -> FileResponse:
        return product_index()

    @app.get("/images")
    def legacy_images() -> Response:
        return RedirectResponse(url="/materials", status_code=307)

    @app.get("/projects/{project_id}/images")
    def legacy_project_images(project_id: str) -> Response:
        return RedirectResponse(url=f"/projects/{project_id}/materials", status_code=307)

    @app.get("/projects/{project_id}/{section}")
    def product_project_section(
        project_id: str,
        section: Literal["overview", "brief", "materials", "shot-images", "plan", "script", "review", "assistant", "tasks"],
    ) -> Response:
        if section == "tasks":
            return RedirectResponse(
                url=f"/tasks?project_id={project_id}", status_code=307
            )
        return product_index()

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse(url="/projects", status_code=307)

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    def api_not_found(path: str) -> None:
        raise HTTPException(status_code=404, detail="Not Found")

    @app.get("/{path:path}", include_in_schema=False)
    def product_not_found(path: str) -> Response:
        return FileResponse(UI_ROOT / "not-found.html", status_code=404)

    return app


app = create_app()


def existing_ui_running() -> bool:
    try:
        opener = build_opener(ProxyHandler({}))
        health_host = "127.0.0.1" if HOST in {"0.0.0.0", "::"} else HOST
        with opener.open(f"http://{health_host}:{PORT}/api/health", timeout=0.8) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("service") == "mplus-launch-ui"
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    load_local_secrets()
    parser = argparse.ArgumentParser(description="Loomtake 多类别脚本本地图形界面")
    parser.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--diagnose", action="store_true", help="只检查本机运行条件，不启动界面")
    args = parser.parse_args(argv)
    if args.diagnose:
        checks = system_checks()
        for check in checks:
            print(f"[{check['status'].upper()}] {check['name']}: {check['message']}")
        failed = [check for check in checks if check["status"] == "fail"]
        print(f"诊断完成：{len(checks) - len(failed)}/{len(checks)} 通过")
        return 2 if failed else 0
    url = PUBLIC_URL
    if existing_ui_running():
        if not args.no_open:
            webbrowser.open(url)
        print(f"Loomtake脚本台已在运行: {url}")
        return 0
    if not args.no_open:
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()
    import uvicorn
    from .formal_runtime import build_formal_runtime, uploaded_document

    print(f"Loomtake脚本台: {url}")
    formal = build_formal_runtime()
    runtime_app = create_app(
        agent_api=formal.agent,
        formal_brief_adapter=formal.brief_adapter,
        formal_brief_upload_factory=uploaded_document,
        formal_generation_service=formal.generation,
        formal_script_service=formal.script_workspace,
        formal_image_service=formal.image_workspace,
        formal_review_service=formal.review_workspace,
        formal_admin_service=formal.admin,
    )
    uvicorn.run(runtime_app, host=HOST, port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
