from __future__ import annotations

import base64
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
from threading import Thread
from urllib.parse import parse_qs, quote, unquote, urlparse
import unittest

from playwright.sync_api import sync_playwright

from tests.support.formal_control_evidence import FormalControlEvidenceRecorder


UI_ROOT = Path(__file__).parents[1] / "mvp_v2" / "ui"
PROJECT_ID = "匿名项目"
IMAGE_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9WlFK0EAAAAASUVORK5CYII=")


def _project() -> dict:
    return {"project_id": PROJECT_ID, "name": "匿名发布会", "project_version": 7}


def _standard(status: str, *, no_references: bool = False, incomplete: bool = False) -> dict:
    if status == "unavailable":
        return {"status": "unavailable", "needs_clarification": True, "reason": "requires_reimport", "message": "请重新导入资料以生成标准卡", "confirmed_at": None}
    result = {
        "status": status,
        "confirmed_at": "2026-08-13T10:00:00Z" if status == "confirmed" else None,
        "tone_keywords": [{"value": "克制明亮", "sources": ["匿名资料.pdf · 第 2 页"]}],
        "expression_focus": [{"value": "让产品功能成为叙事中心", "sources": ["匿名资料.xlsx · 规划 Sheet · 第 12 行"]}],
        "avoid_expressions": [{"value": "避免夸张承诺", "sources": ["匿名资料.docx · 第 4 页"]}],
        "references": [] if no_references else [{"value": "舞台光线参考", "sources": ["匿名资料.pptx · 第 3 张"]}],
    }
    if incomplete:
        result["tone_keywords"] = []
        result["needs_clarification"] = True
        result["missing_fields"] = ["tone_keywords"]
    return result


def _brief_response(standard: dict, *, confirmation_status: str = "confirmed", conflict_open: bool = False) -> dict:
    return {
        "status": "ready",
        "brief_key": "opaque-browser-brief",
        "confirmation_status": confirmation_status,
        "creative_standard": standard,
        "routing": {
            "status": "ready", "raw_script_type": "活动短视频",
            "specific_script_type": "发布会活动回顾视频", "script_type": "发布会",
            "category_id": "launch", "source_workbook_name": "M+发布会类通用脚本库.xlsx",
            "authority": "industry_or_activity_category", "message": "生成将采用发布会模板；资料原文保持不变。",
        },
        "brief": {
            "documents": [{"filename": "匿名资料.pdf", "status": "succeeded", "locations": ["第 2 页"]}],
            "facts": [{"value": "项目名称：匿名发布会", "sources": ["匿名资料.pdf · 第 2 页"]}],
            "conflicts": [{"conflict_key": "conflict-browser", "resolution_status": "unresolved" if conflict_open else "resolved", "choices": [{"fact_key": "fact-browser", "value": "采用匿名发布会"}]}] if conflict_open else [],
        },
    }


def _preview(*, conflict: bool = False) -> dict:
    result = {
        "plan_id": "plan-for-browser",
        "plan_sha256": "a" * 64,
        "status": "waiting_user",
        "events": [
            {"sequence": 1, "event": "开场建立现场期待"},
            {"sequence": 2, "event": "产品首次亮相"},
            {"sequence": 3, "event": "核心功能演示"},
        ],
        "requirements": [{
            "requirement_id": "mandatory_required_001",
            "text": "核心功能演示",
            "kind": "mandatory_required",
            "mandatory": True,
            "evidence_refs": [{"source": {"file": "匿名资料.pdf", "page": 2}}],
        }],
        "coverage": [{
            "requirement_id": "mandatory_required_001",
            "kind": "mandatory_required",
            "status": "covered",
            "shot_nos": ["3"],
            "evidence_refs": [{"source": {"file": "匿名资料.pdf", "page": 2}}],
        }],
        "capacity_report": {
            "status": "sufficient",
            "target_seconds": 60,
            "required_shot_units": 3,
            "planned_shot_count": 3,
            "choices": [],
        },
        "traceability": [{
            "requirement_id": "mandatory_required_001",
            "brief_text": "核心功能演示",
            "classification": "mandatory_content",
            "position_rule": {"type": "none"},
            "plan_shot_nos": ["3"],
            "output_evidence": [],
            "template_source": ["product_launch.xlsx#演示镜头"],
            "review_findings": [],
            "final_status": "covered",
            "decision_source": "brief",
        }],
        "shot_count": 3,
        "total_duration_seconds": 60,
        "shots": [
            {"shot_no": "1", "primary_event": "开场建立现场期待", "visual": "观众入场与舞台灯光渐亮", "sound": "环境声渐入主音乐", "duration_seconds": 18, "source": {"file": "匿名资料.pdf", "page": 2}},
            {"shot_no": "2", "primary_event": "产品首次亮相", "visual": "主讲人揭开产品主体", "sound": "音乐停顿后进入掌声", "duration_seconds": 22, "source": {"file": "匿名资料.xlsx", "sheet": "规划", "row": 12}},
            {"shot_no": "3", "primary_event": "核心功能演示", "visual": "屏幕展示关键使用场景", "sound": "旁白说明与现场反馈", "duration_seconds": 20, "source": {"file": "匿名资料.pptx", "slide": 4}},
        ],
        "template_rule_contract": {"status": "legacy_default", "warning_codes": ["template_rules_legacy_default"]},
        "conflicts": [],
        "decision_counts": {
            "requirements_uncovered": 0,
            "hard_template_downgrades": 0,
            "unresolved_conflicts": 0,
            "unexplained_overrides": 0,
        },
        "audit": {"status": "passed"},
    }
    for shot in result["shots"]:
        shot["field_decisions"] = [{
            "field": "structure",
            "value_status": "resolved",
            "final_value": "发布会结构",
            "candidates": [{
                "source": "template_rule", "value": "发布会结构",
                "rule_id": "launch.structure.browser", "rule_level": "structural_required",
            }],
            "selected_source": "template_rule",
            "decision_status": "applied",
            "override_reason": "",
        }]
    if conflict:
        result["needs_clarification"] = True
        result["audit"] = {"status": "needs_clarification"}
        result["conflicts"] = [{
            "conflict_id": "conflict-browser-hard-slot",
            "status": "unresolved",
            "shot_no": "1",
            "field": "structure",
            "brief_value": "开头核心发言",
            "template_value": "氛围造镜",
            "rule_id": "launch.structure.01",
        }]
        result["decision_counts"]["unresolved_conflicts"] = 1
    return result


def _completed() -> dict:
    return {
        "status": "completed",
        "script_rows": [
            {"镜号": "1", "画面描述": "观众入场与舞台灯光渐亮", "声音": "环境声渐入主音乐", "时长(秒)": 18},
            {"镜号": "2", "画面描述": "主讲人揭开产品主体", "声音": "音乐停顿后进入掌声", "时长(秒)": 22},
            {"镜号": "3", "画面描述": "屏幕展示关键使用场景", "声音": "旁白说明与现场反馈", "时长(秒)": 20},
        ],
    }


class _Fixture:
    def __init__(self, *, standard_status: str = "confirmed", no_references: bool = False, incomplete: bool = False, waiting: bool = False, conflict_on_plan: bool = False, plan_needs_clarification: bool = False, formal_setup_fails: bool = False, image_status: str | None = None, image_workspace: bool = False, image_error_code: str | None = None, image_understand_error_code: str | None = None, default_image_partial: bool = False):
        self.standard_status = standard_status
        self.brief_confirmation_status = "confirmed"
        self.brief_conflict_open = False
        self.no_references = no_references
        self.incomplete = incomplete
        self.waiting = waiting
        self.conflict_on_plan = conflict_on_plan
        self.plan_needs_clarification = plan_needs_clarification
        self.formal_setup_fails = formal_setup_fails
        self.plan_posts = 0
        self.confirm_posts = 0
        self.conflict_prepare_posts = 0
        self.conflict_execute_posts = 0
        self.provider_calls = 0
        self.image_status = image_status
        self.image_error_code = image_error_code
        self.image_understand_error_code = image_understand_error_code
        self.image_analysis_posts = 0
        self.image_understand_posts = 0
        self.image_workspace_enabled = image_workspace
        self.default_image_partial = default_image_partial
        self.default_image_adopted = False
        self.default_image_proposal_posts = 0
        self.default_image_confirm_posts = 0
        self.image_version_posts = 0
        self.supplemented_images = []
        self.supplement_payloads = []
        self.project_version = 7
        self.image_selections = ["candidate-1", "candidate-2", "candidate-3"]
        self.script_model = None
        self.requests: list[tuple[str, str]] = []

    def request_count(self, route: str) -> int:
        return sum(1 for _method, request_route in self.requests if request_route == route)

    def brief(self) -> dict:
        return _brief_response(
            _standard(self.standard_status, no_references=self.no_references, incomplete=self.incomplete),
            confirmation_status=self.brief_confirmation_status,
            conflict_open=self.brief_conflict_open,
        )

    def image_readiness(self) -> dict:
        if self.default_image_adopted:
            return {
                "total": 2,
                "pass": 2,
                "pending": 0,
                "low_confidence": 0,
                "blocked": 0,
                "failed": 0,
                "images": [{
                    "image_key": f"default-candidate-{index}",
                    "status": "pass",
                    "summary": {
                        "caption": f"已采用匿名类别图片 {index}",
                        "tags": ["匿名"],
                        "subjects": ["嘉宾"],
                        "scene": "匿名活动现场",
                        "purpose": "逐镜配图",
                        "quality": "清晰可用",
                        "brand": "无",
                    },
                    "sources": [{"label": "匿名类别库.xlsx", "sheet": "素材", "row": index}],
                } for index in range(1, 3)],
            }
        if self.image_workspace_enabled:
            return {
                "total": 4, "pass": 4, "pending": 0,
                "low_confidence": 0, "blocked": 0, "failed": 0,
                "images": [{
                    "image_key": f"candidate-{index}",
                    "status": "pass",
                    "summary": {
                        "caption": f"匿名候选图片 {index}",
                        "tags": ["发布会", f"镜头{index}"],
                        "subjects": ["嘉宾", "产品"],
                        "scene": "匿名活动现场",
                        "purpose": "逐镜脚本配图",
                        "quality": "清晰可用",
                        "brand": "匿名品牌",
                        "evidence_status": "verified",
                    },
                    "sources": [{"label": f"匿名资料-{index}.png", "image": index}],
                } for index in range(1, 5)],
            }
        if self.image_status is None:
            return {"total": 0, "pass": 0, "pending": 0, "low_confidence": 0, "blocked": 0, "failed": 0, "images": []}
        summary = None if self.image_status == "pending" else {
            "caption": "匿名发布会主舞台", "tags": ["发布", "产品"], "subjects": ["产品", "主讲人"],
            "scene": "主舞台", "purpose": "说明产品发布重点", "quality": "画面清晰",
            "brand": "匿名品牌", "evidence_status": "verified",
        }
        counts = {"pass": 0, "pending": 0, "low_confidence": 0, "blocked": 0, "failed": 0}
        counts[self.image_status] = 1
        return {
            "total": 1, **counts,
            "images": [{
                "image_key": "opaque-browser-image", "status": self.image_status, "summary": summary,
                "sources": [{"label": "05-reference.png", "image": 1}],
            }],
        }

    def image_workspace(self) -> dict:
        if not self.image_workspace_enabled:
            return {"state": "empty", "message": "当前项目还没有脚本版本"}
        candidates = [{
            "image_key": f"candidate-{index}",
            "caption": f"匿名候选图片 {index}",
            "tags": ["发布会", f"镜头{index}"],
            "subjects": ["嘉宾", "产品"],
            "scene": "匿名活动现场",
            "purpose": "逐镜脚本配图",
            "quality": "清晰可用",
            "brand": "匿名品牌",
            "sources": [{"label": f"匿名资料-{index}.png", "image": index}],
        } for index in range(1, 5)]
        visuals = ["观众入场与舞台灯光渐亮", "主讲人揭开产品主体", "屏幕展示关键使用场景"]
        confirmed = self.image_version_posts > 0
        return {
            "state": "confirmed" if confirmed else "ready",
            "project_name": "匿名发布会",
            "version_label": f"版本 {1 + self.image_version_posts}",
            "base_version_id": "version-browser-current",
            "project_version": self.project_version,
            "shot_count": 3,
            "available_image_count": 4,
            "shortage_count": 0,
            "ready_to_save": True,
            "message": "逐镜图片已确认，可继续审核" if confirmed else "请逐镜检查默认配图并确认",
            "shots": [{
                "shot_id": f"shot-{index + 1}",
                "shot_no": str(index + 1),
                "visual": visuals[index],
                "selected_image_key": self.image_selections[index],
                "confirmed": confirmed,
                "candidate_snapshot_digest": "a" * 64,
                "candidates": candidates,
            } for index in range(3)],
            "history": [{
                "version_no": 1,
                "label": "版本 1",
                "is_current": not confirmed,
                "change_summary": "脚本内容版本",
                "created_at": "2026-08-17T00:00:00Z",
            }] + ([{
                "version_no": 2,
                "label": "版本 2",
                "is_current": True,
                "change_summary": "已确认 3 镜图片",
                "created_at": "2026-08-17T00:01:00Z",
            }] if confirmed else []),
            "production_certification": "BLOCK",
        }

    def response(self, method: str, path: str) -> tuple[int, dict | bytes]:
        parsed = urlparse(path)
        route = unquote(parsed.path)
        self.requests.append((method, route))
        if route == "/api/formal/categories":
            from mvp_v2.script_type_adapter import SCRIPT_TYPE_METADATA
            return 200, {"items": [{"category_id": item.category_id, "label": item.display_name} for item in SCRIPT_TYPE_METADATA]}
        if route == f"/api/formal/projects/{PROJECT_ID}/templates":
            return 200, {"project_id": PROJECT_ID, "project_version": 7, "category_id": "launch",
                         "selected_ref": "anonymous-template@1", "is_bound": False, "can_trial": False,
                         "items": [{"ref": "anonymous-template@1", "label": "发布会通用模板", "version": 1,
                                    "source_name": "M+发布会类通用脚本库.xlsx", "shot_count": 3,
                                    "structures": [{"name": "开场", "start": 1, "end": 3, "shot_count": 3}],
                                    "warnings": [], "is_default": True, "selected": True}],
                         "notice": "当前使用类别默认模板。"}
        if route == "/api/bootstrap":
            return 200, {"token": "legacy-ui-token", "capabilities": {"formal_brief": True}, "defaults": {"source_root": "/legacy/private/source", "asset_packages": {}, "asset_package": "", "output_dir": "/legacy/private/output", "max_loops": 3, "model_profile": "legacy-model"}, "system_checks": [{"name": "legacy system check", "status": "pass", "message": "legacy raw check"}], "asset_index_stats": {}, "brief_presets": {}}
        if route == "/api/ui/session":
            return 200, {"token": "product-ui-token"}
        if route == "/api/formal/setup":
            if self.formal_setup_fails:
                return 200, {"enabled": True, "brief_adapter": "BLOCK", "formal_plan": "BLOCK"}
            return 200, {"enabled": True, "brief_adapter": "ready", "formal_plan": "BLOCK", "script_availability": "available"}
        if route == "/api/agent/setup":
            return 200, {"enabled": True}
        if route in {"/api/agent/sessions", "/api/agent/sessions/ui00-session"}:
            return 200, {"session": {"agent_session_id": "ui00-session", "project_id": PROJECT_ID},
                         "tasks": [], "timeline": []}
        if route.startswith("/api/agent/projects"):
            return 200, {"items": []}
        if route == "/api/formal/workspaces/browser-workspace":
            return 200, {"workspace_version": 1}
        if route == "/api/formal/workspaces/browser-workspace/projects":
            return 200, {"items": [_project()]}
        if route == "/api/formal/workspaces/browser-workspace/tasks":
            return 200, {
                "count": 1,
                "items": [{
                    "task_id": "task-browser",
                    "project_id": PROJECT_ID,
                    "project_name": "匿名发布会",
                    "task_type": "formal_generation",
                    "status": "blocked",
                    "error_code": "SCRIPT_TYPE_UNSUPPORTED",
                    "title": "规划与脚本生成",
                    "state_label": "已阻断",
                    "next_action": "请按页面提示补齐资料或处理冲突",
                    "issue": "Brief 中的脚本类型尚不支持，请返回 Brief 补充可用类型。",
                    "action_label": "查看并处理",
                    "action_href": f"/projects/{PROJECT_ID}/brief",
                    "created_at": "2026-08-19T10:00:00Z",
                    "updated_at": "2026-08-19T10:00:01Z",
                }],
            }
        if route == f"/api/formal/projects/{PROJECT_ID}":
            return 200, {**_project(), "project_version": self.project_version}
        if route == f"/api/formal/projects/{PROJECT_ID}/overview":
            return 200, {
                "project_id": PROJECT_ID,
                "project_status": "active",
                "stages": [
                    {"id": "brief", "label": "Brief 与资料", "state": "已完成", "detail": "资料已确认，可继续处理项目。"},
                    {"id": "materials", "label": "项目素材", "state": "已完成", "detail": "已有 1 张图片可用于规划。"},
                    {"id": "planning", "label": "规划与生成", "state": "未开始", "detail": "确认事件和镜头安排。"},
                    {"id": "script", "label": "脚本与版本", "state": "未开始", "detail": "创建并保存脚本版本。"},
                    {"id": "shot_images", "label": "逐镜配图", "state": "未开始", "detail": "为成稿镜头逐镜确认图片。"},
                    {"id": "review", "label": "审核与交付", "state": "未开始", "detail": "逐项审核并确认整片。"},
                ],
                "blockers": [],
                "next_action": {"label": "进入规划与生成", "href": f"/projects/{PROJECT_ID}/plan", "reason": "先查看事件和镜头安排。"},
                "related_tasks": [],
            }
        if route == f"/api/formal/projects/{PROJECT_ID}/lifecycle":
            return 200, {
                "project_id": PROJECT_ID,
                "status": "active",
                "project_version": self.project_version,
                "archive_available": True,
                "restore": {"available": False, "reason": "PROJECT_NOT_ARCHIVED", "remaining_days": 0},
                "purge_available": False,
            }
        if route == f"/api/formal/projects/{PROJECT_ID}/planning-readiness":
            return 200, {"ready": True, "code": "", "message": "Brief 规划字段已核对。"}
        if route == f"/api/formal/projects/{PROJECT_ID}/brief/current":
            return 200, self.brief()
        if route == f"/api/formal/projects/{PROJECT_ID}/script":
            if self.script_model is not None:
                return 200, self.script_model
            has_script = self.image_workspace_enabled or self.default_image_partial
            return 200, {
                "state": "ready" if has_script else "empty", "project_name": "匿名发布会",
                "project_version": self.project_version, "base_version_id": "version-browser-current" if has_script else "",
                "revision": 0, "can_undo": False, "can_redo": False, "can_save_version": False,
                "pending_confirmations": [], "versions": [],
                "shots": [{**shot, "shot_id": f"shot-{index + 1}", "structure": "发布会结构"}
                          for index, shot in enumerate(_preview()["shots"])] if has_script else [],
            }
        if route == f"/api/formal/projects/{PROJECT_ID}/brief/files":
            self.standard_status = "draft"
            self.brief_confirmation_status = "pending"
            self.brief_conflict_open = True
            return 200, self.brief()
        if route == f"/api/formal/projects/{PROJECT_ID}/brief/conflicts/conflict-browser/resolve":
            self.standard_status = "draft"
            self.brief_conflict_open = False
            return 200, self.brief()
        if route == f"/api/formal/projects/{PROJECT_ID}/brief/confirm":
            self.standard_status = "confirmed"
            self.brief_confirmation_status = "confirmed"
            self.incomplete = False
            return 200, self.brief()
        if route == f"/api/formal/projects/{PROJECT_ID}/images":
            if self.image_error_code:
                return 409, {
                    "detail": {
                        "code": self.image_error_code,
                        "message": "当前项目没有已确认 Brief 快照",
                    },
                }
            readiness = self.image_readiness()
            readiness["images"].extend(self.supplemented_images)
            readiness["total"] += len(self.supplemented_images)
            readiness["pending"] += len(self.supplemented_images)
            return 200, readiness
        if route == f"/api/formal/projects/{PROJECT_ID}/image-workspace/supplements" and method == "POST":
            if parse_qs(parsed.query).get("expected_project_version") != [str(self.project_version)]:
                return 409, {"detail": {"code": "PROJECT_VERSION_CONFLICT"}}
            image_key = f"supplement-browser-{len(self.supplemented_images) + 1}"
            self.supplemented_images.append({
                "image_key": image_key, "status": "pending", "summary": None,
                "sources": [{"label": "匿名新增图片.png", "image": 1}],
            })
            self.project_version += 1
            return 200, {
                "project_version": self.project_version, "image_key": image_key,
                "requires_analysis": True, "brief_reupload_required": False,
            }
        if route.startswith(f"/api/formal/projects/{PROJECT_ID}/images/supplement-browser-") and route.endswith("/preview"):
            return 200, IMAGE_BYTES
        if route == f"/api/formal/projects/{PROJECT_ID}/images/understand" and method == "POST":
            self.image_understand_posts += 1
            if self.image_understand_error_code:
                return 503, {
                    "detail": {
                        "code": self.image_understand_error_code,
                        "message": "自动图片理解未完成，原图仍保持待处理，请重试或改为人工理解。",
                    },
                }
            self.image_status = "low_confidence"
            value = self.image_readiness()
            value["automatic_understanding"] = {
                "processed": 1, "remaining": 0, "needs_review": 1,
                "failed": 0, "has_more": False,
            }
            return 200, value
        if route == f"/api/formal/projects/{PROJECT_ID}/images/opaque-browser-image/analysis" and method == "POST":
            self.image_analysis_posts += 1
            self.image_status = "pass"
            return 200, self.image_readiness()
        if route == f"/api/formal/projects/{PROJECT_ID}/images/opaque-browser-image/preview":
            return 200, IMAGE_BYTES
        if route == f"/api/formal/projects/{PROJECT_ID}/images/default-candidates" and self.default_image_partial:
            candidates = [{
                "candidate_key": f"default-candidate-{index}",
                "caption": f"匿名安全类别图片 {index}",
                "source": {"file": "匿名类别库.xlsx", "sheet": "素材", "row": index},
                "preview_url": f"/api/formal/projects/{PROJECT_ID}/images/default-candidates/default-candidate-{index}/preview",
            } for index in range(1, 3)]
            return 200, {
                "state": "partial",
                "project_version": self.project_version,
                "shot_count": 3,
                "ready_shot_count": 2,
                "gap_shot_count": 1,
                "warnings": ["1 镜没有安全类别候选；可先采用其余 2 镜，缺口仍会阻断审核和交付"],
                "shots": [
                    {
                        "state": "ready",
                        "shot_id": "shot-1",
                        "shot_no": "1",
                        "visual": "匿名镜头一",
                        "selected_candidate_key": "default-candidate-1",
                        "candidates": [candidates[0]],
                        "warnings": [],
                    },
                    {
                        "state": "ready",
                        "shot_id": "shot-2",
                        "shot_no": "2",
                        "visual": "匿名镜头二",
                        "selected_candidate_key": "default-candidate-2",
                        "candidates": [candidates[1]],
                        "warnings": [],
                    },
                    {
                        "state": "gap",
                        "shot_id": "shot-3",
                        "shot_no": "3",
                        "visual": "匿名集体合影镜头",
                        "selected_candidate_key": "",
                        "candidates": [],
                        "gap_reason": "缺少必须画面主体: 集体合影",
                        "warnings": [],
                        "supplement_upload_available": True,
                        "temporary_text_basis_available": True,
                    },
                ],
            }
        if route.startswith(f"/api/formal/projects/{PROJECT_ID}/images/default-candidates/default-candidate-") and route.endswith("/preview"):
            return 200, IMAGE_BYTES
        if route.startswith(f"/api/formal/projects/{PROJECT_ID}/images/default-candidate-") and route.endswith("/preview"):
            return 200, IMAGE_BYTES
        if route == f"/api/formal/projects/{PROJECT_ID}/images/default-proposals" and method == "POST" and self.default_image_partial:
            self.default_image_proposal_posts += 1
            return 200, {
                "task_id": "default-image-proposal",
                "status": "proposed",
                "response": {
                    "type": "action_proposal",
                    "summary": "为 2 镜采用 2 张已校验类别图片",
                    "proposal_digest": "d" * 64,
                    "will_change": "采用已通过镜头",
                    "warnings": ["仍有 1 镜未完成图片决定；审核和交付继续阻断"],
                },
            }
        if route == "/api/agent/tasks/default-image-proposal/confirm" and method == "POST" and self.default_image_partial:
            self.default_image_confirm_posts += 1
            self.default_image_adopted = True
            return 200, {
                "task": {"status": "completed"},
                "response": {"type": "formal_default_images_adopted"},
            }
        if route.startswith(f"/api/formal/projects/{PROJECT_ID}/images/candidate-") and route.endswith("/preview"):
            return 200, IMAGE_BYTES
        if route == f"/api/formal/projects/{PROJECT_ID}/image-workspace/progress":
            current = self.image_workspace()
            return 200, {"base_version_id": current.get("base_version_id"), "project_version": self.project_version,
                "revision": 0, "script_revision": 0, "selections": [], "saved_at": "", "shot_count": len(current.get("shots", [])),
                "selected_count": 0, "pending_count": len(current.get("shots", [])), "needs_check_count": 0}
        if route == f"/api/formal/projects/{PROJECT_ID}/image-workspace/assistant" and method == "GET":
            return 200, {"task": None, "response": None}
        if route == f"/api/formal/projects/{PROJECT_ID}/image-workspace":
            return 200, self.image_workspace()
        if route == f"/api/formal/projects/{PROJECT_ID}/image-workspace/proposals" and method == "POST":
            return 200, {
                "proposal_id": "image-proposal-browser",
                "proposal_digest": "b" * 64,
                "affected_shots": ["1", "2", "3"],
                "rerun_nodes": ["visual_binding", "workbook_render", "machine_audit"],
                "review_eligible": True,
            }
        if route == f"/api/formal/projects/{PROJECT_ID}/image-workspace/proposals/image-proposal-browser/confirm" and method == "POST":
            self.image_version_posts += 1
            self.project_version += 2
            return 200, self.image_workspace()
        if route == f"/api/formal/projects/{PROJECT_ID}/plans":
            if method == "POST":
                self.plan_posts += 1
                if self.conflict_on_plan:
                    self.conflict_on_plan = False
                    return 409, {"detail": {"code": "PROJECT_VERSION_CONFLICT", "message": "project changed"}}
                self.waiting = True
                preview = _preview(conflict=self.plan_needs_clarification)
                if self.plan_needs_clarification:
                    preview["audit"] = {"status": "needs_clarification"}
                return 200, preview
            return 200, {"items": [{"plan_id": "plan-for-browser", "status": "waiting_user"}] if self.waiting else []}
        if route == f"/api/formal/projects/{PROJECT_ID}/plans/plan-for-browser":
            if method == "POST" and route.endswith("/confirm"):
                self.confirm_posts += 1
                return 200, _completed()
            preview = _preview(conflict=self.plan_needs_clarification)
            if self.plan_needs_clarification:
                preview["audit"] = {"status": "needs_clarification"}
            return 200, preview
        if route == f"/api/formal/projects/{PROJECT_ID}/plans/plan-for-browser/conflict-resolutions/prepare":
            self.conflict_prepare_posts += 1
            return 200, {
                "command_token": "manual-command.browser-confirmation-token",
                "risk": "R2",
                "model_calls": 0,
                "will_change": "保存冲突决定并立即重新规划。",
                "will_not_change": "不覆盖原 Brief、原 Plan 或历史 run。",
                "confirmation_label": "我已核对全部冲突",
            }
        if route == f"/api/formal/projects/{PROJECT_ID}/plans/plan-for-browser/conflict-resolutions/execute":
            self.conflict_execute_posts += 1
            self.plan_needs_clarification = False
            resolved = _preview()
            resolved["plan_id"] = "plan-resolved-browser"
            resolved["plan_sha256"] = "b" * 64
            resolved["decision_counts"]["hard_template_downgrades"] = 1
            return 200, {
                "new_plan": resolved,
                "model_calls": 0,
                "script_versions_created": 0,
            }
        if route == f"/api/formal/projects/{PROJECT_ID}/plans/plan-for-browser/confirm":
            self.confirm_posts += 1
            self.provider_calls += 2
            self.waiting = False
            return 200, _completed()
        if route == f"/api/formal/projects/{PROJECT_ID}/tasks":
            return 200, {
                "count": 1,
                "items": [{
                    "title": "规划与脚本生成",
                    "state": "blocked",
                    "state_label": "已阻断",
                    "next_action": "请按页面提示补齐资料或处理冲突",
                    "issue": "Brief 中的脚本类型尚不支持，请返回 Brief 补充可用类型。",
                    "action_label": "查看并处理",
                    "action_href": f"/projects/{PROJECT_ID}/brief",
                    "created_at": "2026-08-19T10:00:00Z",
                    "updated_at": "2026-08-19T10:00:01Z",
                }],
                "production_certification": "BLOCK",
            }
        return 404, {"detail": {"code": "NOT_FOUND", "message": "fixture route missing"}}


def _handler(fixture: _Fixture):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def do_GET(self):
            if self.path.startswith("/api/"):
                return self._api("GET")
            self._file()

        def do_POST(self):
            self._api("POST")

        def do_PATCH(self):
            self._api("PATCH")

        def _api(self, method: str):
            if method == "POST" and unquote(urlparse(self.path).path) == f"/api/formal/projects/{PROJECT_ID}/image-workspace/supplements":
                fixture.supplement_payloads.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            status, payload = fixture.response(method, self.path)
            raw = isinstance(payload, bytes)
            body = payload if raw else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "image/png" if raw else "application/json; charset=utf-8")
            if raw:
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self):
            route = urlparse(self.path).path
            if route.startswith("/assets/"):
                target = UI_ROOT / route.removeprefix("/assets/")
            elif route in {"/", "/projects", "/projects/new", "/tasks", "/assets", "/knowledge", "/system", "/materials", "/shot-images", "/tools"} or route.startswith("/projects/"):
                target = UI_ROOT / "index.html"
            else:
                target = UI_ROOT / route.lstrip("/")
            if not target.is_file():
                self.send_error(404)
                return
            content = target.read_bytes()
            content_type = "text/javascript; charset=utf-8" if target.suffix == ".js" else "text/css; charset=utf-8" if target.suffix == ".css" else "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    return Handler


class Ui00JourneyThreeTests(unittest.TestCase):
    def test_project_creation_defers_category_to_brief_and_uses_server_metadata(self):
        html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
        script = (UI_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="formal-project-category"', html)
        self.assertIn('api("/api/formal/categories")', script)
        creation_markup = html.split('id="formal-project-create"', 1)[1].split("</form>", 1)[0]
        self.assertIn('id="formal-project-name"', creation_markup)
        self.assertNotIn('id="formal-project-category"', creation_markup)
        brief_markup = html.split('id="page-brief"', 1)[1].split('id="page-plan"', 1)[0]
        self.assertIn('id="formal-project-category"', brief_markup)
        create_function = script.split("async function createFormalProject(", 1)[1].split("async function uploadFormalBrief(", 1)[0]
        self.assertIn("name: elements.formalProjectName.value.trim()", create_function)
        self.assertNotIn("category_id", create_function)
        self.assertNotIn('category_id: "launch"', script)

    def test_project_creation_cancel_returns_to_projects_and_hides_form(self):
        fixture = _Fixture()
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{base}/projects/new")
            page.wait_for_load_state("networkidle")
            self.assertTrue(page.locator("#formal-project-create").is_visible())

            page.locator("#formal-project-create-cancel").click()
            page.wait_for_url(f"{base}/projects")
            page.wait_for_load_state("networkidle")

            self.assertTrue(page.locator("#formal-project-create").is_hidden())
            self.assertTrue(page.locator("#formal-project-list").is_visible())
            self.assertEqual(errors, [])
            context.close()
            browser.close()

    def test_images_without_confirmed_brief_show_actionable_prerequisite(self):
        fixture = _Fixture(image_error_code="CURRENT_BRIEF_MISSING")
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 375)
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(f"{base}/projects/{PROJECT_ID}/materials")
            page.wait_for_load_state("networkidle")

            self.assertEqual(
                page.locator("#formal-image-gate-title").inner_text(),
                "请先完成并确认需求",
            )
            visible = page.locator("#page-materials").inner_text()
            self.assertIn("请先在需求页导入并确认资料", visible)
            self.assertIn("等待需求确认", visible)
            self.assertNotIn("操作没有完成", visible)
            self.assertNotIn("请刷新页面后重试", visible)
            self.assertEqual(page_errors, [])
            context.close()
            browser.close()

    def test_image_analysis_provider_failure_keeps_pending_state_and_explains_retry(self):
        fixture = _Fixture(image_status="pending", image_understand_error_code="FORMAL_VISION_PROVIDER_FAILED")
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            page.goto(f"{base}/projects/{PROJECT_ID}/materials")
            page.wait_for_load_state("networkidle")
            self.assertEqual(page.locator("#formal-image-pending").inner_text(), "1")
            page.locator("#formal-image-understand").click()
            page.wait_for_function("state.formalImageAutoBusy === false")
            notification = page.locator("#toast")
            notification.wait_for(state="visible")
            visible = notification.inner_text()
            self.assertIn("自动图片理解未完成", visible)
            self.assertIn("原图仍保持待处理", visible)
            self.assertIn("请重试或改为人工理解", visible)
            self.assertEqual(page.locator("#formal-image-pending").inner_text(), "1")
            self.assertTrue(page.locator("#formal-image-understand").is_enabled())
            context.close()
            browser.close()

    def test_project_materials_allow_multi_image_upload_and_explain_reconfirmation(self):
        fixture = _Fixture(image_status="pass")
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{base}/projects/{PROJECT_ID}/materials")
            page.wait_for_load_state("networkidle")

            upload = page.locator("#formal-image-upload-files")
            self.assertTrue(upload.get_attribute("multiple") is not None)
            self.assertIn(".jpg", upload.get_attribute("accept") or "")
            self.assertIn(".png", upload.get_attribute("accept") or "")
            self.assertIn(".webp", upload.get_attribute("accept") or "")
            self.assertTrue(page.locator("#formal-image-upload-form").is_visible())
            self.assertIn("可一次上传多张", page.locator("#page-materials").inner_text())
            original_brief = fixture.brief()
            supplement_requests = []
            page.on("request", lambda request: supplement_requests.append(request)
                    if request.method == "POST" and "/image-workspace/supplements?" in request.url else None)

            upload.set_input_files([
                {"name": "qingyu-hero.jpg", "mimeType": "image/jpeg", "buffer": IMAGE_BYTES},
                {"name": "qingyu-stage.webp", "mimeType": "image/webp", "buffer": IMAGE_BYTES},
            ])
            self.assertIn("已选择 2 张图片", page.locator("#formal-image-upload-selection").inner_text())
            page.locator("#formal-image-upload-button").click()
            page.locator("#formal-image-upload-state", has_text="已加入 2 张图片。").wait_for()
            self.assertIn("点击“分析图片”后即可选用", page.locator("#formal-image-upload-state").inner_text())
            self.assertEqual(fixture.request_count(f"/api/formal/projects/{PROJECT_ID}/image-workspace/supplements"), 2)
            self.assertEqual([parse_qs(urlparse(request.url).query)["expected_project_version"] for request in supplement_requests], [["7"], ["8"]])
            self.assertEqual(len(fixture.supplement_payloads), 2)
            for payload, filename in zip(fixture.supplement_payloads, ("qingyu-hero.jpg", "qingyu-stage.webp")):
                self.assertIn(f'filename="{filename}"'.encode(), payload)
                self.assertIn(IMAGE_BYTES, payload)
            self.assertEqual(fixture.request_count(f"/api/formal/projects/{PROJECT_ID}/brief/files"), 0)
            self.assertEqual(fixture.brief(), original_brief)
            self.assertEqual(unquote(urlparse(page.url).path), f"/projects/{PROJECT_ID}/script")
            self.assertEqual(parse_qs(urlparse(page.url).query), {"panel": ["images"], "aux": ["materials"]})
            self.assertTrue(page.locator("#page-materials").is_visible())
            self.assertEqual(page.locator("#formal-image-pending").inner_text(), "2")
            self.assertTrue(page.locator("#formal-image-understand").is_enabled())
            self.assertEqual(errors, [])
            context.close()
            browser.close()

    def test_partial_default_images_show_gap_and_adopt_only_ready_shots(self):
        fixture = _Fixture(default_image_partial=True)
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            errors: list[str] = []
            page.on(
                "console",
                lambda message: errors.append(message.text) if message.type == "error" else None,
            )
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{base}/projects/{PROJECT_ID}/materials")
            page.wait_for_load_state("networkidle")

            page.locator("[data-load-default-images]").click()
            page.locator("[data-library-view='recommended']").click()
            page.locator("[data-library-scope]").select_option("all")
            page.locator("[data-default-image-gap='3']").wait_for()
            visible = page.locator("[data-default-image-panel]").inner_text()
            self.assertIn("缺少必须画面主体: 集体合影", visible)
            self.assertIn("先采用已选图片，其他镜头稍后补充", visible)
            self.assertEqual(page.locator("[data-default-image-select]").count(), 2)
            screenshot_path = os.environ.get("MPLUS_DEFAULT_HOTFIX_SCREENSHOT")
            if screenshot_path:
                page.screenshot(path=screenshot_path, full_page=True)

            self.assertTrue(page.locator("[data-propose-default-images]").is_disabled())
            for index in range(2):
                page.locator(f"[data-default-image-select='{index}']").select_option(f"default-candidate-{index + 1}")
            with page.expect_request("**/images/default-proposals?*") as adoption_request:
                page.locator("[data-propose-default-images]").click()
            self.assertEqual(adoption_request.value.post_data_json["selections"], [
                {"shot_no": "1", "candidate_key": "default-candidate-1"},
                {"shot_no": "2", "candidate_key": "default-candidate-2"},
            ])
            page.locator("[data-confirm-default-images]").wait_for()
            self.assertIn(
                "仍有 1 镜未完成图片决定",
                page.locator("[data-default-image-panel]").inner_text(),
            )
            page.locator("[data-confirm-default-images]").click()
            page.locator("#formal-image-total", has_text="2").wait_for()
            self.assertEqual(fixture.default_image_proposal_posts, 1)
            self.assertEqual(fixture.default_image_confirm_posts, 1)
            self.assertEqual(errors, [])
            context.close()
            browser.close()

    def _server(self, fixture: _Fixture):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fixture))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_port}"

    @staticmethod
    def _page(browser, width: int):
        context = browser.new_context(viewport={"width": width, "height": 900})
        page = context.new_page()
        page.add_init_script("localStorage.setItem('mplus.agent.auth', 'browser-session'); localStorage.setItem('mplus.agent.workspace', 'browser-workspace');")
        return context, page

    def test_standard_card_absence_blocks_without_plan_request(self):
        fixture = _Fixture(standard_status="unavailable")
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{base}/projects/{PROJECT_ID}/plan?mode=text-only")
            page.wait_for_load_state("networkidle")
            self.assertTrue(page.locator("#formal-plan-standard-block").is_visible())
            self.assertFalse(page.locator("#formal-plan-create").is_visible())
            self.assertIn("需重新导入资料生成标准卡", page.locator("#page-plan").inner_text())
            self.assertEqual(fixture.plan_posts, 0)
            self.assertEqual(fixture.confirm_posts, 0)
            self.assertEqual(fixture.provider_calls, 0)
            self.assertEqual(errors, [])
            context.close()

            browser.close()

    def test_task_record_explains_block_and_opens_recovery_page(self):
        fixture = _Fixture()
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            page.goto(f"{base}/tasks?project_id={quote(PROJECT_ID)}")
            page.wait_for_load_state("networkidle")
            self.assertEqual(page.locator("#formal-task-list .task-compact-row").count(), 1)
            self.assertIn("脚本类型尚不支持", page.locator("#formal-task-list").inner_text())
            action = page.locator("#formal-task-list a", has_text="查看并处理")
            self.assertEqual(unquote(action.get_attribute("href")), f"/projects/{PROJECT_ID}/brief")
            action.click()
            page.wait_for_url(f"**/projects/{quote(PROJECT_ID)}/brief")
            self.assertTrue(page.locator("#page-brief").is_visible())
            context.close()
            browser.close()

    def test_sidebar_navigation_labels_stay_horizontal(self):
        fixture = _Fixture()
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1280)
            page.goto(f"{base}/projects")
            page.wait_for_load_state("networkidle")
            items = page.locator(".workspace-nav .nav-item:visible")
            self.assertGreater(items.count(), 0)
            for item in items.all():
                self.assertLessEqual(item.bounding_box()["height"], 50)
                self.assertGreater(item.locator("span").bounding_box()["width"], 100)
            context.close()
            browser.close()

    def test_mobile_project_menu_closes_on_escape(self):
        fixture = _Fixture()
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 375)
            page.goto(f"{base}/projects/{PROJECT_ID}/brief")
            page.wait_for_load_state("networkidle")
            menu = page.locator("#formal-mobile-menu")
            menu.locator("summary").click()
            self.assertTrue(menu.evaluate("node => node.open"))
            page.keyboard.press("Escape")
            self.assertFalse(menu.evaluate("node => node.open"))
            context.close()
            browser.close()

    def test_mobile_project_controls_have_touch_target_height(self):
        fixture = _Fixture()
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 375)
            page.goto(f"{base}/projects/{PROJECT_ID}/brief")
            page.wait_for_load_state("networkidle")
            for selector in ("#formal-mobile-exit", "#formal-mobile-menu summary"):
                box = page.locator(selector).bounding_box()
                self.assertIsNotNone(box)
                self.assertGreaterEqual(box["height"], 44, selector)
            context.close()
            browser.close()

    def test_script_and_image_closure_renders_authoritative_dto_fields(self):
        fixture = _Fixture()
        fixture.script_model = {
            "state": "ready", "project_name": "匿名项目", "project_version": 7,
            "base_version_id": "version-browser-current", "revision": 0,
            "can_undo": False, "can_redo": False, "can_save_version": False,
            "pending_confirmations": [], "versions": [],
            "shots": [{
                "shot_no": "1", "shot_id": "shot-1", "assistant_locked": True,
                "camera_type": "低机位", "shot_size": "中景", "camera_move": "缓推",
                "image_reconfirmation_required": True, "structure": "开场",
                "duration_seconds": 3, "visual": "匿名画面", "sound": "", "voiceover": "",
                "subtitle": "", "notes": "", "source": {"file": "fixture.xlsx", "sheet": "脚本", "row": 2},
            }],
        }
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1280)
            page.goto(f"{base}/projects/{PROJECT_ID}/script")
            page.wait_for_load_state("networkidle")
            page.locator("#creative-protection-details > summary").click()
            self.assertIn("已锁定 1 镜", page.locator("#formal-script-lock-summary").inner_text())
            self.assertIn("低机位", page.locator("[data-script-shot-id='shot-1']").inner_text())
            self.assertIn("图片需重新确认", page.locator("[data-script-shot-id='shot-1']").inner_text())
            self.assertFalse(page.locator("[data-script-field='visual']").is_disabled())
            page.locator("#formal-script-lock-edit").click()
            self.assertTrue(page.locator("[data-script-lock-shot='shot-1']").is_checked())
            self.assertFalse(page.locator("[data-script-field='visual']").is_disabled())

            def lock_route(route):
                locked = set(route.request.post_data_json["locked_shot_ids"])
                fixture.project_version += 1
                fixture.script_model["project_version"] = fixture.project_version
                for shot in fixture.script_model["shots"]:
                    shot["assistant_locked"] = shot["shot_id"] in locked
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(fixture.script_model, ensure_ascii=False))

            page.route("**/script/assistant-locks*", lock_route)
            with page.expect_request("**/script/assistant-locks*") as lock_request:
                page.locator("#formal-script-lock-save").click()
            self.assertEqual(lock_request.value.method, "PUT")
            self.assertEqual(lock_request.value.post_data_json["expected_project_version"], 7)
            self.assertEqual(lock_request.value.post_data_json["locked_shot_ids"], ["shot-1"])
            self.assertTrue(lock_request.value.post_data_json["idempotency_key"])

            page.locator("#formal-script-lock-edit").click()
            page.locator("[data-script-lock-shot='shot-1']").uncheck()
            with page.expect_request("**/script/assistant-locks*") as unlock_request:
                page.locator("#formal-script-lock-save").click()
            self.assertEqual(unlock_request.value.post_data_json["locked_shot_ids"], [])
            page.locator("#creative-assistant > summary").click()
            page.locator("#formal-script-instruction").fill("调整所选镜头节奏")
            page.route("**/script/draft-proposals*", lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps({
                    "task_id": "analysis-task-1", "response": {
                        "type": "scope_required", "analysis_task_id": "analysis-task-1",
                        "summary": "请选择建议范围",
                        "suggested_shots": [{"shot_id": "shot-1", "shot_no": "1", "label": "镜头 1"}],
                    },
                }, ensure_ascii=False),
            ))
            page.locator("#formal-script-instruction-submit").click()
            page.locator("#formal-script-scope").wait_for()
            self.assertTrue(page.locator("#formal-script-scope").is_visible())
            self.assertTrue(page.locator("[data-script-scope-shot]").is_checked())
            page.route("**/script/draft-proposals*", lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "task_id": "proposal-task-1",
                    "response": {
                        "type": "action_proposal", "summary": "可执行修改",
                        "draft_only": True, "proposal_digest": "digest-1",
                        "preview": {"before": "修改前", "after": "修改后"},
                    },
                }, ensure_ascii=False),
            ))
            with page.expect_request("**/script/draft-proposals*") as scope_request:
                page.locator("#formal-script-scope-continue").click()
            self.assertEqual(scope_request.value.post_data_json["analysis_task_id"], "analysis-task-1")
            self.assertEqual(scope_request.value.post_data_json["selected_shot_ids"], ["shot-1"])

            # 逐镜配图与图库选择由创作工作台的专用浏览器旅程覆盖；本用例只验证脚本锁定与助手范围 DTO。
            context.close()
            browser.close()

    def test_script_save_failure_keeps_input_and_reuses_idempotency_key(self):
        fixture = _Fixture()
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1280)
            page.goto(f"{base}/projects")
            page.wait_for_load_state("networkidle")
            page.evaluate("""projectId => {
              window.history.replaceState({}, '', `/projects/${projectId}/script`);
              state.formalScriptProjectId = projectId;
              state.formalScriptAvailable = true;
              showFormalPage('script');
              elements.formalScriptProjectRequired.hidden = true;
              elements.formalScriptWorkbench.hidden = false;
              renderFormalScript({
                state: 'ready', project_name: '匿名项目', project_version: 7,
                revision: 0, can_undo: false, can_redo: false, can_save_version: false,
                pending_confirmations: [], versions: [],
                shots: [{
                  shot_no: '1', shot_id: 'shot-1', structure: '开场', duration_seconds: 3,
                  visual: '原始画面', sound: '原始声音', voiceover: '原始旁白',
                  subtitle: '原始字幕', notes: '', source: {file: 'fixture.xlsx', sheet: '脚本', row: 2},
                }],
              });
            }""", PROJECT_ID)
            requests = []

            def save_route(route):
                if route.request.method != "POST":
                    route.continue_()
                    return
                payload = route.request.post_data_json
                requests.append(payload)
                if len(requests) == 1:
                    route.abort()
                    return
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "state": "ready", "project_name": "匿名项目", "project_version": 7,
                        "revision": 1, "can_undo": True, "can_redo": False,
                        "can_save_version": False, "pending_confirmations": [], "versions": [],
                        "shots": [{
                            "shot_no": "1", "shot_id": "shot-1", "structure": "开场",
                            "duration_seconds": 3, "visual": "保存后的画面", "sound": "原始声音",
                            "voiceover": "原始旁白", "subtitle": "原始字幕", "notes": "",
                            "source": {"file": "fixture.xlsx", "sheet": "脚本", "row": 2},
                        }],
                    }, ensure_ascii=False),
                )

            page.route("**/script/table*", save_route)
            cell = page.locator("[data-script-shot='1'][data-script-field='visual']")
            cell.fill("网络失败后仍保留的画面")
            page.locator("#formal-script-retry", has_text="重试保存草稿").wait_for()
            self.assertEqual(cell.input_value(), "网络失败后仍保留的画面")
            self.assertTrue(page.locator("#formal-script-discard").is_enabled())
            self.assertTrue(page.locator("#formal-script-retry").is_visible())
            page.locator("#formal-script-retry").click()
            page.wait_for_function("state.formalScriptSaveInFlight === false && state.formalScriptDirtyCells.size === 0")
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0]["idempotency_key"], requests[1]["idempotency_key"])
            self.assertEqual(cell.input_value(), "网络失败后仍保留的画面")
            context.close()
            browser.close()

    def test_script_save_queue_reuses_keys_only_for_live_operations(self):
        probe = Path(__file__).parent / "support" / "formal_script_save_queue_probe.cjs"
        result = subprocess.run(
            ["node", str(probe)],
            cwd=Path(__file__).parents[1],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "STEP_PASS|Q1-operation-key-is-per-save-operation",
                "STEP_PASS|Q2-unsent-revert-does-not-block-other-cell",
                "STEP_PASS|Q3-inflight-old-value-cancels-stale-queued-value",
            ],
        )

    def test_plan_shell_keeps_required_states_and_safe_employee_copy(self):
        page = (UI_ROOT / "index.html").read_text(encoding="utf-8")
        script = (UI_ROOT / "app.js").read_text(encoding="utf-8")
        styles = (UI_ROOT / "app.css").read_text(encoding="utf-8")
        self.assertIn('id="page-plan"', page)
        self.assertIn('id="formal-plan-standard-block"', page)
        self.assertIn('id="formal-script-pending"', page)
        self.assertIn("请先完成并确认项目创作标准卡", page)
        self.assertIn("formalPlanCollectionPath", script)
        self.assertIn("formalPlanPreviewPath", script)
        self.assertIn("currentBrief?.creative_standard", script)
        self.assertNotIn("creative_standard_card", script)
        self.assertIn("PROJECT_VERSION_CONFLICT", script)
        self.assertIn("state.formalPlanState !== \"idle\"", script)
        self.assertIn("state.formalPlanState !== \"waiting_user\"", script)
        self.assertIn("elements.formalPlanConfirmation.focus", script)
        self.assertIn("model?.pending_confirmations", script)
        for state in ("idle", "loading", "blocked", "waiting_user", "generating", "completed"):
            self.assertIn(f"{state}:", script)
        plan_markup = page.split('<section id="page-plan"', 1)[1].split('<section id="page-shot-images"', 1)[0].casefold()
        for technical_copy in ("artifact", "sha", "provider", "model", "rawjson"):
            self.assertNotIn(technical_copy, plan_markup)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles)
        self.assertIn(".ui00-plan-shots", styles)
        self.assertIn(".ui00-standard-grid", styles)
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(48px, 1fr))", styles)
        self.assertIn(".project-nav { display: none; }", styles)
        self.assertIn(".mobile-project-menu:not([hidden])", styles)
        self.assertIn('preview.loading = "lazy"', script)
        self.assertIn("preview.width = 1280", script)
        self.assertIn("preview.height = 720", script)
        self.assertNotIn('用途\"></textarea>', script)

    def test_zero_image_text_only_mode_requires_explicit_route_choice(self):
        fixture = _Fixture()
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            page.goto(f"{base}/projects/{PROJECT_ID}/plan")
            page.wait_for_load_state("networkidle")
            self.assertTrue(page.locator("#formal-plan-image-block").is_visible())
            self.assertEqual(page.locator("#formal-plan-create").inner_text(), "选择文字草稿")
            self.assertEqual(fixture.plan_posts, 0)
            choice = page.locator("#formal-plan-image-block a", has_text="仅生成文字草稿")
            choice_url = urlparse(choice.get_attribute("href"))
            self.assertEqual(unquote(choice_url.path), f"/projects/{PROJECT_ID}/script")
            self.assertEqual(parse_qs(choice_url.query), {"mode": ["text-only"], "panel": ["structure"]})
            choice.click()
            page.wait_for_url("**/script?mode=text-only&panel=structure")
            page.locator("#formal-plan-create", has_text="生成文字规划").wait_for()
            self.assertEqual(fixture.plan_posts, 0)
            self.assertEqual(fixture.provider_calls, 0)
            with page.expect_request(lambda request: request.method == "POST" and urlparse(request.url).path.endswith("/plans")) as planned:
                page.locator("#formal-plan-create").click()
            page.locator("#formal-plan-confirmation").wait_for()
            self.assertEqual(planned.value.post_data_json["generation_mode"], "text_only_draft")
            self.assertEqual(fixture.plan_posts, 1)
            self.assertEqual(fixture.provider_calls, 0)
            context.close()
            browser.close()

    def test_project_images_route_reviews_pending_image_and_unlocks_plan(self):
        fixture = _Fixture(image_status="pending")
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: errors.append(f"HTTP {response.status}: {response.url}") if response.status >= 400 else None)
            page.goto(f"{base}/projects/{PROJECT_ID}/materials")
            page.wait_for_load_state("networkidle")

            self.assertEqual(page.locator("#formal-image-total").inner_text(), "1")
            self.assertEqual(page.locator("#formal-image-pending").inner_text(), "1")
            self.assertIn("请先处理图片", page.locator("#formal-image-gate-title").inner_text())
            self.assertEqual(page.locator("#formal-image-grid .ui00-image-card").count(), 1)
            page.locator("#formal-image-preview-0 img").wait_for()
            self.assertEqual(page.locator("#formal-image-to-plan").get_attribute("aria-disabled"), "true")
            page.locator(".ui00-image-sources summary").click()
            self.assertIn("05-reference.png · 图片 1", page.locator(".ui00-image-sources").inner_text())
            page.locator("#formal-image-understand").click()
            page.locator("#toast", has_text="需要人工核对").wait_for()
            self.assertEqual(fixture.image_understand_posts, 1)
            self.assertEqual(page.locator("#formal-image-pending").inner_text(), "0")
            self.assertIn("需要人工核对", page.locator("#toast").inner_text())
            self.assertTrue(page.evaluate("document.activeElement?.classList.contains('ui00-image-card')"))

            page.goto(f"{base}/projects/{PROJECT_ID}/plan")
            page.wait_for_load_state("networkidle")
            self.assertTrue(page.locator("#formal-plan-image-block").is_visible())
            self.assertFalse(page.locator("#formal-plan-create").is_visible())
            self.assertEqual(fixture.plan_posts, 0)
            self.assertEqual(page.evaluate("state.formalImagePreviewUrls.size"), 0)
            page.go_back()
            page.wait_for_url(f"**/projects/{quote(PROJECT_ID)}/script?panel=images&aux=materials")
            page.locator("#formal-image-preview-0 img").wait_for()

            page.locator(".ui00-image-review summary").click()
            form = page.locator("[data-formal-image-form='0']")
            form.locator("[name='caption']").fill("匿名发布会主舞台")
            form.locator("[name='tags']").fill("发布、产品")
            form.locator("[name='subjects']").fill("产品、主讲人")
            form.locator("[name='scene']").fill("主舞台")
            form.locator("[name='purpose']").fill("说明产品发布重点")
            form.locator("[name='quality']").fill("画面清晰")
            form.locator("[name='brand']").fill("匿名品牌")
            form.locator("[name='evidence']").fill("已对照原始资料核对")
            form.locator("button[type='submit']").click()
            page.locator("#formal-image-state", has_text="全部可用").wait_for()

            self.assertEqual(fixture.image_analysis_posts, 1)
            self.assertTrue(page.evaluate("document.activeElement?.classList.contains('ui00-image-card')"))
            self.assertEqual(page.locator("#formal-image-passed").inner_text(), "1")
            self.assertEqual(page.locator("#formal-image-pending").inner_text(), "0")
            self.assertEqual(page.locator("#formal-image-attention").inner_text(), "0")
            self.assertEqual(page.locator("#formal-image-state").inner_text(), "全部可用")
            self.assertEqual(page.locator("#formal-image-gate-copy").inner_text(), "已完成 1 张图片分析，可以进入规划")
            self.assertIsNone(page.locator("#formal-image-to-plan").get_attribute("aria-disabled"))
            self.assertIn("匿名发布会主舞台", page.locator("#page-materials").inner_text())
            page.reload()
            page.wait_for_load_state("networkidle")
            self.assertEqual(page.locator("#formal-image-state").inner_text(), "全部可用")
            self.assertEqual(page.locator("#formal-image-passed").inner_text(), "1")

            visible = page.locator("#page-materials").inner_text().casefold()
            for forbidden in ("token", "workspace", "actor", "raw json", "artifact", "sha", "provider", "model", "path", "cache", "node", "current", "opaque-browser-image"):
                self.assertNotIn(forbidden, visible)
            self.assertGreaterEqual(page.locator("#page-materials label[for]").count(), 9)
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
            self.assertEqual(fixture.plan_posts, 0)
            self.assertEqual(fixture.provider_calls, 0)
            self.assertEqual(fixture.request_count("/api/bootstrap"), 0)

            page.locator("#formal-image-to-plan").click()
            page.wait_for_url(f"**/projects/{quote(PROJECT_ID)}/script?panel=structure")
            page.locator("#formal-plan-create").wait_for()
            self.assertEqual(page.evaluate("state.formalImagePreviewUrls.size"), 0)
            self.assertEqual(page.evaluate("state.formalImageThumbnailUrls.size"), 1)
            self.assertTrue(page.locator("#page-materials").is_hidden())
            page.locator("#formal-plan-back-brief").click()
            page.wait_for_url(f"**/projects/{quote(PROJECT_ID)}/brief")
            self.assertEqual(page.evaluate("state.formalImagePreviewUrls.size"), 0)
            self.assertEqual(page.evaluate("state.formalImageThumbnailUrls.size"), 0)
            self.assertEqual(fixture.plan_posts, 0)
            self.assertEqual(errors, [])
            context.close()

            context, page = self._page(browser, 1024)
            errors = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: errors.append(f"HTTP {response.status}: {response.url}") if response.status >= 400 else None)
            page.goto(f"{base}/projects/{PROJECT_ID}/shot-images")
            page.wait_for_load_state("networkidle")
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
            self.assertEqual(errors, [])
            context.close()
            browser.close()

    def test_project_images_route_confirms_one_unique_image_per_shot_and_recovers(self):
        from tests.test_mvp_v2_creative_workspace_browser import _CreativeFixture
        fixture = _CreativeFixture()
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            def capture_body(route):
                if route.request.method in {"POST", "PATCH"}:
                    fixture.body = route.request.post_data_json or {}
                route.continue_()
            page.route("**/api/**", capture_body)
            page.goto(f"{base}/projects/{PROJECT_ID}/shot-images")
            page.wait_for_load_state("networkidle")
            page.locator("#creative-image-scope").select_option("all")
            self.assertEqual(page.locator("#formal-image-shot-grid [data-image-shot]").count(), 3)
            self.assertEqual(page.locator(".ui00-image-shot-preview img").count(), 3)
            self.assertTrue(page.locator("#formal-image-version-save").is_disabled())
            page.locator("[data-image-shot-select='1']").select_option("candidate-4")
            for index in range(3):
                page.locator(f"[data-image-shot-use='{index}']").click()
            self.assertEqual(
                page.locator("#formal-image-selection-status").inner_text(),
                "全稿 3 镜 · 已选用 3 镜 · 未配图 0 镜 · 已确认待保存 3 镜 · 本次修改 3 镜 · 可以查看统一保存预览",
            )
            self.assertFalse(page.locator("#formal-image-version-save").is_disabled())
            page.locator("#formal-image-version-save").click()
            page.locator("[data-confirm-script-revision]").click()
            page.locator("#formal-script-save-proposal", has_text="文字与配图已保存为一个新版本").wait_for()
            page.locator('[data-creative-panel="images"]').click()
            page.locator("#formal-image-version-state", has_text="已确认").wait_for()
            self.assertEqual(fixture.image_version_posts, 1)
            self.assertTrue(page.evaluate("document.activeElement?.matches('[data-creative-panel=images]')"))
            self.assertEqual(page.locator("#formal-image-version-history .ui00-image-version-item").count(), 2)
            page.reload()
            page.wait_for_load_state("networkidle")
            self.assertEqual(page.locator("[data-image-shot-confirm]:checked").count(), 3)
            self.assertEqual(page.locator("#formal-image-version-state").inner_text(), "已确认")
            visible = page.locator("#page-shot-images").inner_text().casefold()
            for forbidden in (
                "token", "workspace", "actor", "raw json", "artifact", "sha",
                "provider", "model", "path", "cache", "node", "current",
                "candidate-1", "candidate-2", "candidate-3", "candidate-4",
            ):
                self.assertNotIn(forbidden, visible)
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
            self.assertEqual(errors, [])
            context.close()
            browser.close()

    def test_product_startup_uses_only_safe_setup_and_never_falls_back_to_legacy_bootstrap(self):
        fixture = _Fixture()
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{base}/projects/{PROJECT_ID}/brief")
            page.wait_for_load_state("networkidle")
            self.assertEqual(fixture.request_count("/api/bootstrap"), 0)
            self.assertGreaterEqual(fixture.request_count("/api/formal/setup"), 1)
            self.assertEqual(page.evaluate("state.token"), "product-ui-token")
            storage = page.evaluate("({ local: Object.keys(localStorage).sort(), session: Object.keys(sessionStorage).sort() })")
            self.assertEqual(storage, {
                "local": ["mplus.agent.auth", f"mplus.agent.session.{PROJECT_ID}", "mplus.agent.workspace"],
                "session": [f"mplus.brief-inputs.browser-workspace.{PROJECT_ID}", "mplus.last-project"],
            })
            self.assertEqual(page.evaluate("sessionStorage.getItem('mplus.last-project')"), PROJECT_ID)
            visible = page.locator("#page-brief").inner_text().casefold()
            for forbidden in ("legacy-ui-token", "/legacy/private", "legacy-model", "legacy raw check"):
                self.assertNotIn(forbidden, visible)
            page.locator('#formal-project-navigation a[data-route="overview"]').click()
            page.wait_for_url(f"**/projects/{quote(PROJECT_ID)}/overview")
            page.locator("#formal-overview-state").wait_for()
            page.go_back()
            page.wait_for_url(f"**/projects/{quote(PROJECT_ID)}/brief")
            page.locator("#formal-brief-workbench").wait_for()
            page.go_forward()
            page.wait_for_url(f"**/projects/{quote(PROJECT_ID)}/overview")
            page.locator("#formal-overview-state").wait_for()
            page.reload()
            page.wait_for_load_state("networkidle")
            self.assertEqual(fixture.request_count("/api/bootstrap"), 0)
            self.assertEqual(errors, [])
            context.close()

            context, page = self._page(browser, 1024)
            page.goto(f"{base}/projects")
            page.wait_for_load_state("networkidle")
            self.assertEqual(fixture.request_count("/api/bootstrap"), 0)
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
            page.goto(f"{base}/projects/new")
            page.wait_for_load_state("networkidle")
            self.assertEqual(page.locator("#projects-title").inner_text(), "新建项目")
            self.assertEqual(page.locator("#projects-copy").inner_text(), "先填写名称，再进入需求工作区分析与确认。")
            self.assertTrue(page.locator("#formal-project-create").is_visible())
            self.assertTrue(page.locator("#formal-project-toolbar").is_hidden())
            self.assertTrue(page.locator("#formal-project-dashboard").is_hidden())
            page.goto(f"{base}/")
            page.wait_for_load_state("networkidle")
            self.assertEqual(fixture.request_count("/api/bootstrap"), 0)
            self.assertTrue(page.locator("#page-projects").is_visible())
            self.assertEqual(page.locator("#legacy-compatibility").count(), 0)
            context.close()
            browser.close()

    def test_product_safe_setup_failure_stays_business_facing_without_legacy_fallback(self):
        fixture = _Fixture(formal_setup_fails=True)
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1024)
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{base}/projects")
            page.wait_for_load_state("networkidle")
            self.assertEqual(fixture.request_count("/api/formal/setup"), 1)
            self.assertEqual(fixture.request_count("/api/bootstrap"), 0)
            self.assertIn("项目工作区暂不可用，请稍后重试或联系管理员。", page.locator("#formal-project-list").inner_text())
            self.assertEqual(page.evaluate("state.token"), "product-ui-token")
            self.assertEqual(errors, [])
            context.close()
            browser.close()

    def test_brief_standard_card_draft_confirmed_and_no_reference_states(self):
        fixture = _Fixture(standard_status="draft", no_references=True)
        fixture.brief_confirmation_status = "pending"
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{base}/projects/{PROJECT_ID}/brief")
            page.wait_for_load_state("networkidle")
            card = page.locator("#formal-standard-card-details")
            self.assertEqual(card.locator(".ui00-standard-field").count(), 4)
            self.assertIn("调性", card.inner_text())
            self.assertIn("重点", card.inner_text())
            self.assertIn("禁用表达", card.inner_text())
            self.assertIn("客户未提供参考资料", card.inner_text())
            self.assertEqual(page.locator("#formal-brief-route-state").inner_text(), "已识别：发布会")
            page.locator(".template-routing > summary").click()
            self.assertIn("发布会活动回顾视频", page.locator("#formal-brief-route-details").inner_text())
            self.assertIn("M+发布会类通用脚本库.xlsx", page.locator("#formal-brief-route-details").inner_text())
            self.assertIn("采用发布会模板", page.locator("#formal-brief-gate-title").inner_text())
            card.locator("details summary").first.click()
            self.assertIn("匿名资料.pdf · 第 2 页", card.inner_text())
            self.assertFalse(page.locator("#formal-brief-confirm").is_disabled())
            page.locator("#formal-brief-confirm").click()
            page.locator("#formal-standard-card-state", has_text="已确认").wait_for()
            self.assertIn("已确认", page.locator("#formal-brief-gate-title").inner_text())
            self.assertEqual(card.locator(".ui00-standard-field").count(), 0)
            self.assertNotIn("查看来源", card.inner_text())
            self.assertNotIn("匿名资料.pdf", card.inner_text())
            self.assertNotIn("克制明亮", card.inner_text())
            page.reload()
            page.wait_for_load_state("networkidle")
            self.assertEqual(page.locator("#formal-standard-card-state").inner_text(), "已确认")
            self.assertEqual(card.locator(".ui00-standard-field").count(), 0)
            self.assertEqual(card.inner_text(), "")
            visible = page.locator("#page-brief").inner_text().casefold()
            for forbidden in ("token", "workspace", "actor", "raw json", "ref", "sha", "model", "provider", "path", "cache", "node", "current"):
                self.assertNotIn(forbidden, visible)
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
            self.assertEqual(errors, [])
            context.close()
            browser.close()

    def test_brief_incomplete_and_legacy_standard_block_confirmation_and_plan(self):
        for fixture, expected in (
            (_Fixture(standard_status="draft", incomplete=True), "请补充调性后再确认"),
            (_Fixture(standard_status="unavailable"), "需重新导入资料生成标准卡"),
        ):
            base = self._server(fixture)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context, page = self._page(browser, 1024)
                page.goto(f"{base}/projects/{PROJECT_ID}/brief")
                page.wait_for_load_state("networkidle")
                self.assertTrue(page.locator("#formal-brief-confirm").is_disabled())
                self.assertIn(expected, page.locator("#page-brief").inner_text())
                page.goto(f"{base}/projects/{PROJECT_ID}/plan?mode=text-only")
                page.wait_for_load_state("networkidle")
                self.assertTrue(page.locator("#formal-plan-standard-block").is_visible())
                self.assertEqual(fixture.plan_posts, 0)
                self.assertEqual(fixture.provider_calls, 0)
                self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
                context.close()
                browser.close()

    def test_brief_upload_and_conflict_resolution_immediately_refresh_standard_card(self):
        fixture = _Fixture(standard_status="unavailable")
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            page.goto(f"{base}/projects/{PROJECT_ID}/brief")
            page.wait_for_load_state("networkidle")
            page.locator("#formal-brief-dropzone").evaluate("""zone => {
                const transfer = new DataTransfer();
                transfer.items.add(new File(['anonymous'], '拖拽资料.txt', {type: 'text/plain'}));
                zone.dispatchEvent(new DragEvent('drop', {bubbles: true, dataTransfer: transfer}));
            }""")
            self.assertIn("拖拽资料.txt", page.locator("#formal-brief-selection").inner_text())
            page.locator("#formal-brief-upload-button").click()
            page.locator("#formal-standard-card-state", has_text="待确认").wait_for()
            self.assertTrue(page.locator("#formal-brief-confirm").is_disabled())
            page.locator("#formal-conflict-list button", has_text="确认选择").click()
            page.wait_for_function("document.querySelector('#formal-brief-confirm')?.disabled === false")
            self.assertFalse(page.locator("#formal-brief-confirm").is_disabled())
            context.close()
            browser.close()

    def test_plan_mock_journey_recovers_waiting_and_creates_nonempty_preview(self):
        fixture = _Fixture()
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{base}/projects/{PROJECT_ID}/plan?mode=text-only")
            page.wait_for_load_state("networkidle")
            self.assertTrue(page.locator("#formal-plan-create").is_visible())
            self.assertEqual(page.locator("#formal-plan-confirm").is_disabled(), True)
            page.locator("#formal-plan-create").click()
            page.locator("#formal-plan-confirmation").wait_for()
            self.assertEqual(fixture.plan_posts, 1)
            self.assertIn("3 镜／60 秒", page.locator("#formal-plan-summary").inner_text())
            self.assertEqual(page.locator("#formal-plan-decision-counts button").count(), 4)
            self.assertIsNone(page.locator("#formal-plan-capacity-details").get_attribute("open"))
            page.locator("#formal-plan-capacity-details > summary").click()
            self.assertIn("需求未覆盖", page.locator("#formal-plan-decision-counts").inner_text())
            self.assertIsNone(page.locator("#formal-plan-shots-details").get_attribute("open"))
            page.locator("#formal-plan-shots-details > summary").click()
            page.locator("#formal-plan-shots .ui00-plan-shot").nth(1).locator("details").first.locator("summary").click()
            self.assertIn("匿名资料.xlsx · 规划 Sheet · 第 12 行", page.locator("#formal-plan-shots").inner_text())
            page.locator("#formal-plan-shots .ui00-plan-shot").first.locator(".ui00-plan-field-panel > summary").click()
            page.locator("#formal-plan-shots .ui00-plan-shot").first.locator(".ui00-plan-field-panel details > summary").first.click()
            plan_copy = page.locator("#formal-plan-shots").inner_text()
            self.assertIn("模板规则", plan_copy)
            self.assertNotIn("launch.structure.browser", plan_copy)
            self.assertIn("机器校验已完成，非阻断提示留待人工核对", page.locator("#formal-plan-audit").inner_text())
            page.reload()
            page.wait_for_load_state("networkidle")
            self.assertTrue(page.locator("#formal-plan-confirmation").is_visible())
            self.assertEqual(page.locator("#formal-plan-shots .ui00-plan-shot").count(), 3)
            page.locator("#formal-plan-confirm-check").check()
            page.locator("#formal-plan-confirm").click()
            page.locator("#formal-plan-result").wait_for()
            self.assertEqual(fixture.confirm_posts, 1)
            self.assertEqual(fixture.provider_calls, 2)
            self.assertEqual(page.locator("#formal-plan-script-rows tbody tr").count(), 3)
            self.assertTrue(page.locator("#formal-plan-to-script").get_attribute("href").endswith(f"/{quote(PROJECT_ID)}/script"))
            visible = page.locator("#page-plan").inner_text().casefold()
            for forbidden in ("token", "workspace", "actor", "rawjson", "sha", "current", "artifact", "provider", "model", "path", "cache", "node"):
                self.assertNotIn(forbidden, visible)
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
            self.assertEqual(errors, [])
            context.close()

            context, page = self._page(browser, 1024)
            errors = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{base}/projects/{PROJECT_ID}/plan?mode=text-only")
            page.wait_for_load_state("networkidle")
            self.assertTrue(page.locator("#formal-plan-create").is_visible())
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
            self.assertEqual(errors, [])
            context.close()

            context, page = self._page(browser, 390)
            errors = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{base}/projects/{PROJECT_ID}/plan?mode=text-only")
            page.wait_for_load_state("networkidle")
            page.locator("#formal-plan-create").click()
            page.locator("#formal-plan-coverage table").wait_for()
            self.assertIn("核心功能演示", page.locator("#formal-plan-coverage").inner_text())
            self.assertIn("需求与镜头对应", page.locator("#formal-plan-coverage").inner_text())
            page.locator("#formal-plan-capacity-details > summary").click()
            self.assertIn("容量可执行", page.locator("#formal-plan-capacity").inner_text())
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
            self.assertEqual(errors, [])
            context.close()
            browser.close()

    def test_plan_needs_clarification_stays_at_preview_until_user_action(self):
        fixture = _Fixture(plan_needs_clarification=True)
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            page.goto(f"{base}/projects/{PROJECT_ID}/plan?mode=text-only")
            page.wait_for_load_state("networkidle")
            page.locator("#formal-plan-create").click()
            page.locator("#formal-plan-preview").wait_for()
            self.assertIn("机器校验提示仍需处理", page.locator("#formal-plan-audit").inner_text())
            self.assertTrue(page.locator("#formal-plan-confirmation").is_visible())
            self.assertTrue(page.locator("#formal-plan-confirm-check").is_disabled())
            self.assertTrue(page.locator("#formal-plan-confirm").is_disabled())
            self.assertEqual(fixture.confirm_posts, 0)
            self.assertEqual(fixture.provider_calls, 0)
            context.close()
            browser.close()

    def test_hard_template_conflict_requires_two_step_manual_confirmation(self):
        fixture = _Fixture(plan_needs_clarification=True)
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 390)
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{base}/projects/{PROJECT_ID}/plan?mode=text-only")
            page.wait_for_load_state("networkidle")
            page.locator("#formal-plan-create").click()
            page.locator("#formal-plan-conflicts").wait_for()
            self.assertIn("开头核心发言", page.locator("#formal-plan-conflicts").inner_text())
            self.assertIn("氛围造镜", page.locator("#formal-plan-conflicts").inner_text())
            self.assertEqual(page.locator('#formal-plan-decision-counts [data-count-key="unresolved_conflicts"] strong').inner_text(), "1")
            page.locator("#formal-plan-conflict-prepare").click()
            self.assertEqual(fixture.conflict_prepare_posts, 0)
            page.locator('#formal-plan-conflict-list input[value="brief_wins"]').check()
            page.locator("#formal-plan-conflict-prepare").click()
            page.locator("#formal-plan-conflict-confirmation").wait_for()
            self.assertEqual(fixture.conflict_prepare_posts, 1)
            self.assertIn("模型调用 0 次", page.locator("#formal-plan-conflict-confirmation").inner_text())
            self.assertEqual(fixture.conflict_execute_posts, 0)
            FormalControlEvidenceRecorder(
                journey_id="controlled-hard-template-conflict",
            ).capture(
                page, "M20", keyboard_selector="#formal-plan-conflict-check",
                double_click_selector="#formal-plan-conflict-list",
                recovery_selector="#plan-title",
                database_result="受控 HTTP fixture 以 prepare/execute 计数证明确认前零写入",
                terminal_result="结构冲突逐项选择采用资料要求或模板建议，再确认重新规划",
            )
            page.locator("#formal-plan-conflict-check").check()
            page.locator("#formal-plan-conflict-execute").click()
            page.wait_for_function("document.querySelector('#formal-plan-conflicts')?.hidden === true")
            self.assertEqual(fixture.conflict_execute_posts, 1)
            self.assertEqual(fixture.provider_calls, 0)
            self.assertEqual(page.locator('#formal-plan-decision-counts [data-count-key="hard_template_downgrades"] strong').inner_text(), "1")
            self.assertFalse(page.locator("#formal-plan-confirm-check").is_disabled())
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
            self.assertEqual(errors, [])
            context.close()
            browser.close()

    def test_project_version_conflict_refreshes_without_automatic_retry(self):
        fixture = _Fixture(conflict_on_plan=True)
        base = self._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = self._page(browser, 1440)
            page.goto(f"{base}/projects/{PROJECT_ID}/plan?mode=text-only")
            page.wait_for_load_state("networkidle")
            page.locator("#formal-plan-create").click()
            page.locator("#formal-plan-create").wait_for()
            self.assertEqual(fixture.plan_posts, 1)
            self.assertTrue(page.locator("#formal-plan-create").is_visible())
            self.assertIn("可以生成规划", page.locator("#formal-plan-status-copy").inner_text())
            self.assertEqual(fixture.confirm_posts, 0)
            self.assertEqual(fixture.provider_calls, 0)
            context.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()
