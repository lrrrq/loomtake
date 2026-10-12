"""T08 私有图片决定、同版 Excel 与 PDF 交付层。

公共 VersionDeliveryContext 与 VersionQualityEvidence 分别由 T00、T07/R03
所有。本模块只消费它们和受控 resolver 返回的不可变对象，绝不接受调用方
给出的图片池、图片决定或脚本行字典。最终导出要求 T07 返回可解析、持久
artifact-backed 的 shot review 与 final confirmation；阶段一 ``memory://``
原型会被拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zipfile import ZipFile

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Alignment, Font
from PIL import Image as PILImage
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from .excel_io import write_template_output_workbook
from .version_quality_artifacts import VersionQualityArtifactWriter


ArtifactResolver = Callable[[str, str], Path]
AuthorityResolver = Callable[[object], object]


class V10ImageDeliveryError(ValueError):
    pass


@dataclass(frozen=True)
class V10ImageDeliveryArtifact:
    workbook_path: Path
    workbook_sha256: str
    project_id: str
    version_id: str
    run_id: str
    visual_binding_ref: str
    visual_binding_sha256: str
    quality_evidence_sha256: str
    final_reviewer_actor_id: str
    final_confirmed_at: str
    bindings: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class V10PdfDeliveryArtifact:
    pdf_path: Path
    pdf_sha256: str
    workbook_sha256: str
    project_id: str
    version_id: str
    run_id: str
    visual_binding_sha256: str
    quality_evidence_sha256: str
    final_reviewer_actor_id: str
    final_confirmed_at: str


@dataclass(frozen=True)
class FormalSnapshotPdfArtifact:
    pdf_path: Path
    pdf_sha256: str
    workbook_sha256: str
    snapshot_digest: str
    project_id: str
    version_id: str
    run_id: str
    shot_count: int


def create_v10_image_delivery(
    context: object,
    *,
    artifact_resolver: ArtifactResolver,
    quality_artifact_reader: VersionQualityArtifactWriter,
    quality_resolver: object,
    quality_auth_token: str,
    project_image_pool_resolver: AuthorityResolver,
    image_decision_resolver: AuthorityResolver,
    output_root: str | Path,
) -> V10ImageDeliveryArtifact:
    """Create the final Excel only from authority-resolved, SHA-checked inputs."""

    values = _context_values(context)
    payload = _load_visual_binding(values, artifact_resolver)
    script_rows = _script_rows(payload)
    prior_bindings = _binding_by_shot(payload, script_rows)
    images = _image_pool_values(project_image_pool_resolver(context), values, artifact_resolver)
    decisions = _decision_values(
        image_decision_resolver(context), values, script_rows, images, artifact_resolver
    )
    quality = _resolve_quality_values(
        quality_resolver, quality_auth_token, values, artifact_resolver,
        quality_artifact_reader, shot_count=len(script_rows)
    )
    _require_child_version_for_replacements(values, prior_bindings, decisions)
    rows, detail_rows, visual_rows, audit_rows, bindings, local_paths = _delivery_rows(
        values, script_rows, decisions
    )
    root = _delivery_root(output_root)
    workbook_path = root / f"v10-image-delivery-{_snapshot_sha(values, quality, bindings)[:16]}.xlsx"
    write_template_output_workbook(
        workbook_path, rows, detail_rows, audit_rows=audit_rows, visual_asset_rows=visual_rows,
        embed_evidence_previews=True, embed_review_previews=True,
    )
    _replace_main_script_sheet(workbook_path, rows)
    _append_delivery_audit(workbook_path, values, quality, bindings)
    _scrub_runtime_paths(workbook_path, local_paths, root)
    _verify_reopenable_workbook(workbook_path, values, quality, bindings, script_rows, expected_images=len(script_rows), forbidden_paths=local_paths | {root})
    return V10ImageDeliveryArtifact(
        workbook_path, _sha256_file(workbook_path), values["project_id"], values["version_id"],
        values["run_id"], values["visual_binding_ref"], values["visual_binding_sha256"],
        quality["quality_evidence_sha256"], quality["confirmation_reviewer_actor_id"],
        quality["confirmed_at"], tuple(bindings),
    )


def create_v10_pdf_delivery(
    excel: V10ImageDeliveryArtifact, *, output_root: str | Path
) -> V10PdfDeliveryArtifact:
    """Render a PDF from the sealed Excel, retaining only same-version evidence."""

    if not excel.workbook_path.is_file() or _sha256_file(excel.workbook_path) != excel.workbook_sha256:
        raise V10ImageDeliveryError("同版 Excel 不存在或 SHA 已漂移")
    pages = _pdf_pages_from_excel(excel)
    root = _delivery_root(output_root)
    pdf_path = root / f"v10-image-delivery-{excel.workbook_sha256[:16]}.pdf"
    page_count = _write_pdf(pdf_path, excel, pages)
    _verify_pdf(pdf_path, excel, expected_pages=page_count, forbidden_paths={root, excel.workbook_path.parent})
    return V10PdfDeliveryArtifact(
        pdf_path, _sha256_file(pdf_path), excel.workbook_sha256, excel.project_id,
        excel.version_id, excel.run_id, excel.visual_binding_sha256, excel.quality_evidence_sha256,
        excel.final_reviewer_actor_id, excel.final_confirmed_at,
    )


def create_formal_snapshot_pdf(
    workbook_path: str | Path,
    *,
    workbook_sha256: str,
    snapshot_digest: str,
    project_id: str,
    version_id: str,
    run_id: str,
    source_trace: Sequence[Mapping[str, Any]],
    script_rows: Sequence[Mapping[str, Any]],
    output_path: str | Path,
    adaptation_by_shot: Mapping[str, str] | None = None,
) -> FormalSnapshotPdfArtifact:
    """Render optional Formal PDF from the Excel bound to one delivery snapshot."""

    from .excel_io import _script_image_bytes

    workbook_path = Path(workbook_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    if not workbook_path.is_file() or _sha256_file(workbook_path) != workbook_sha256:
        raise V10ImageDeliveryError("Formal snapshot Excel 不存在或 SHA 已漂移")
    if not _is_sha256(snapshot_digest) or not source_trace:
        raise V10ImageDeliveryError("Formal delivery snapshot 或来源追溯不完整")
    workbook = load_workbook(workbook_path, read_only=False, data_only=True)
    try:
        if "脚本" not in workbook.sheetnames:
            raise V10ImageDeliveryError("Formal Excel 缺少脚本工作表")
        script = workbook["脚本"]
        required = {"镜号", "画面描述", "声音"}
        header_row = 0
        headers: dict[str, int] = {}
        observed_headers: list[list[str]] = []
        for candidate in range(1, min(script.max_row, 20) + 1):
            values = {}
            for column in range(1, script.max_column + 1):
                value = str(script.cell(candidate, column).value or "").strip()
                if value:
                    values[value] = column
            if candidate > 1:
                for column in range(1, script.max_column + 1):
                    value = str(script.cell(candidate - 1, column).value or "").strip()
                    if value in {"结构", "镜号"}:
                        values[value] = column
            observed_headers.append(list(values)[:20])
            if not (required - set(values)):
                header_row, headers = candidate, values
                break
        if not header_row:
            raise V10ImageDeliveryError(
                f"Formal Excel 缺少 PDF 所需脚本字段: {sorted(required - set(headers))}; "
                f"observed={observed_headers[:8]}"
            )
        image_column = headers.get("画面示意")
        images = {}
        for image in script._images:
            marker = getattr(image.anchor, "_from", None)
            if marker is None or not header_row < marker.row + 1 <= script.max_row or (
                image_column is not None and marker.col + 1 != image_column
            ):
                continue
            image_row = marker.row + 1
            if image_row in images:
                raise V10ImageDeliveryError("Formal PDF 的同一镜头存在多张主画面图片")
            images[image_row] = True if image_column is not None else image._data()
        saved_images = _script_image_bytes(workbook) if image_column is not None else {}
        trace_by_shot = {str(item.get("shot_no") or ""): item for item in source_trace}
        script_by_shot = {str(item.get("镜号") or item.get("shot_no") or ""): item for item in script_rows}
        pages = []
        for row_index in range(header_row + 1, script.max_row + 1):
            shot_no = str(script.cell(row_index, headers["镜号"]).value or "").strip()
            trace = trace_by_shot.get(shot_no)
            authoritative = script_by_shot.get(shot_no)
            image = saved_images.get(shot_no) if image_column is not None else images.get(row_index)
            if not shot_no or trace is None or authoritative is None or not image:
                raise V10ImageDeliveryError("Formal PDF 的镜号、图片或来源追溯不完整")
            if (
                str(script.cell(row_index, headers["画面描述"]).value or "")
                != str(authoritative.get("画面描述") or "")
                or str(script.cell(row_index, headers["声音"]).value or "")
                != str(authoritative.get("声音") or "")
            ):
                raise V10ImageDeliveryError("Formal Excel 与同 snapshot 脚本 artifact 不一致")
            pages.append({
                "shot_no": shot_no,
                "duration": str(authoritative.get("时长(秒)") or authoritative.get("duration_seconds") or ""),
                "visual": str(authoritative.get("画面描述") or ""),
                "sound": str(authoritative.get("声音") or ""),
                "narration": str(authoritative.get("旁白") or ""),
                "subtitle": str(authoritative.get("字幕") or ""),
                "adaptation": str((adaptation_by_shot or {}).get(shot_no) or ""),
                "image": image,
                "image_sha256": str(trace.get("image_sha256") or ""),
                "source": (str(trace.get("source_file") or "AI 生成图")
                    if trace.get("source_kind") == "generated"
                    else f"{trace.get('source_file')} / {trace.get('source_sheet')} / {trace.get('source_row')}"),
            })
    finally:
        workbook.close()
    if len(pages) != len(source_trace):
        raise V10ImageDeliveryError("Formal PDF 镜头数与 delivery snapshot 不一致")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(output_path), pagesize=A4)
    canvas.setTitle("Loomtake Formal script delivery")
    canvas.setSubject(
        f"project={project_id};version={version_id};run={run_id};"
        f"delivery_snapshot={snapshot_digest};excel_sha256={workbook_sha256}"
    )
    page_count = _render_pdf_pages(canvas, pages, title="Loomtake Formal 同版交付", metadata=(
        f"version={version_id}  run={run_id}",
        f"delivery_snapshot={snapshot_digest}",
        f"excel_sha256={workbook_sha256}",
    ), image_bottom=A4[1] - 390)
    canvas.save()
    reader = PdfReader(str(output_path))
    subject = str((reader.metadata or {}).get("/Subject") or "")
    if len(reader.pages) != page_count or snapshot_digest not in subject or workbook_sha256 not in subject:
        raise V10ImageDeliveryError("Formal PDF 页数或同版 snapshot 元数据不一致")
    raw = output_path.read_bytes()
    if b".mplus" in raw or str(workbook_path.parent).encode() in raw or str(output_path.parent).encode() in raw:
        raise V10ImageDeliveryError("Formal PDF 含本地绝对路径泄露")
    return FormalSnapshotPdfArtifact(
        output_path, _sha256_file(output_path), workbook_sha256, snapshot_digest,
        project_id, version_id, run_id, len(pages),
    )


def _context_values(context: object) -> dict[str, str]:
    _require_object(context, "必须消费 T00 VersionDeliveryContext 对象")
    if _field(context, "schema_version") != "mplus-version-delivery-context/v1":
        raise V10ImageDeliveryError("只接受 T00 mplus-version-delivery-context/v1")
    values = {name: _required_text(context, name, "VersionDeliveryContext") for name in (
        "workspace_id", "project_id", "version_id", "run_id",
    )}
    lock = _field(context, "project_lock_version")
    if isinstance(lock, bool) or not isinstance(lock, int) or lock < 0:
        raise V10ImageDeliveryError("project_lock_version 必须为非负整数")
    values["project_lock_version"] = str(lock)
    values["parent_version_id"] = str(_field(context, "parent_version_id") or "").strip()
    visual = _field(_field(context, "artifacts"), "visual_binding")
    values["visual_binding_ref"] = _required_text(visual, "ref", "VersionDeliveryContext")
    values["visual_binding_sha256"] = _required_text(visual, "sha256", "VersionDeliveryContext")
    _require_artifact(values["visual_binding_ref"], values["visual_binding_sha256"], "visual_binding")
    return values


def _load_visual_binding(values: Mapping[str, str], resolver: ArtifactResolver) -> dict[str, Any]:
    path = _resolved_artifact(resolver, values["visual_binding_ref"], values["visual_binding_sha256"])
    payload = _json_object(path, "visual_binding")
    if payload.get("schema_version") != "mplus-visual-binding/v1":
        raise V10ImageDeliveryError("只接受 mplus-visual-binding/v1")
    return payload


def _image_pool_values(pool: object, context: Mapping[str, str], resolver: ArtifactResolver) -> dict[str, dict[str, str]]:
    _require_object(pool, "项目图片池必须来自 ProjectImagePoolService，不接受调用方字典")
    _require_bound(pool, context, "项目图片池")
    raw_images = _field(pool, "images")
    if not isinstance(raw_images, tuple) or not raw_images:
        raise V10ImageDeliveryError("项目图片池必须为非空不可变 images 元组")
    result: dict[str, dict[str, str]] = {}
    image_shas: set[str] = set()
    for item in raw_images:
        _require_object(item, "项目图片记录必须为正式不可变对象")
        analysis = _field(item, "analysis")
        _require_object(analysis, "图片分析必须为正式不可变对象")
        image = {name: _required_text(item, name, "项目图片记录") for name in (
            "image_id", "image_sha256", "source_document_id", "source_sheet", "source_row",
        )}
        image["source_kind"] = _required_text(item, "source_kind", "项目图片记录")
        image["rights_notice"] = _required_text(item, "rights_notice", "项目图片记录")
        if "placeholder" in image["source_kind"].lower():
            raise V10ImageDeliveryError("项目图片 source_kind 不得为 placeholder 或 test_placeholder")
        image["analysis_ref"] = _required_text(analysis, "ref", "图片分析")
        image["analysis_sha256"] = _required_text(analysis, "sha256", "图片分析")
        controlled = _field(item, "controlled_path")
        if not isinstance(controlled, Path):
            raise V10ImageDeliveryError("项目图片受控路径必须由 ProjectImagePoolService 提供 Path")
        image["controlled_path"] = str(controlled.resolve())
        if image["image_id"] in result or not _is_sha256(image["image_sha256"]):
            raise V10ImageDeliveryError("项目图片 image_id 或 SHA 无效")
        if image["image_sha256"] in image_shas:
            raise V10ImageDeliveryError("项目图片池必须按 image SHA 去重")
        _require_artifact(image["analysis_ref"], image["analysis_sha256"], "图片分析")
        analysis_payload = _json_object(_resolved_artifact(resolver, image["analysis_ref"], image["analysis_sha256"]), "图片分析")
        if (
            analysis_payload.get("status") != "pass"
            or str(analysis_payload.get("image_id") or "") != image["image_id"]
            or str(analysis_payload.get("image_sha256") or "") != image["image_sha256"]
        ):
            raise V10ImageDeliveryError("图片分析 artifact 未通过或与项目图片不一致")
        path = Path(image["controlled_path"])
        if not path.is_file() or _sha256_file(path) != image["image_sha256"]:
            raise V10ImageDeliveryError("受控项目图片不存在或 image SHA 已漂移")
        _require_clear_non_solid_image(path)
        result[image["image_id"]] = image
        image_shas.add(image["image_sha256"])
    return result


def _decision_values(
    snapshot: object,
    context: Mapping[str, str],
    script_rows: Sequence[Mapping[str, Any]],
    images: Mapping[str, dict[str, str]],
    resolver: ArtifactResolver,
) -> dict[str, dict[str, str]]:
    _require_object(snapshot, "人工决定必须来自 ImageDecisionService，不接受调用方字典")
    _require_bound(snapshot, context, "人工决定")
    snapshot_ref = _required_text(snapshot, "snapshot_ref", "人工决定")
    snapshot_sha = _required_text(snapshot, "snapshot_sha256", "人工决定")
    _require_artifact(snapshot_ref, snapshot_sha, "人工决定快照")
    authority = _json_object(_resolved_artifact(resolver, snapshot_ref, snapshot_sha), "人工决定快照")
    authority_rows = authority.get("decisions")
    if not isinstance(authority_rows, list):
        raise V10ImageDeliveryError("人工决定快照缺少 decisions")
    authority_by_id = {
        str(row.get("decision_id") or ""): row for row in authority_rows if isinstance(row, Mapping)
    }
    raw_decisions = _field(snapshot, "decisions")
    if not isinstance(raw_decisions, tuple):
        raise V10ImageDeliveryError("人工决定必须为不可变 decisions 元组")
    result: dict[str, dict[str, str]] = {}
    image_shas: set[str] = set()
    for raw in raw_decisions:
        _require_object(raw, "人工图片决定必须为正式不可变对象")
        decision = {name: _required_text(raw, name, "人工图片决定") for name in (
            "decision_id", "shot_no", "image_id", "actor_id", "decided_at", "resolution_status",
        )}
        if decision["resolution_status"] == "text_only_approved":
            raise V10ImageDeliveryError(f"镜头 {decision['shot_no']} 不允许 text_only_approved")
        if decision["shot_no"] in result:
            raise V10ImageDeliveryError("每镜必须且只能有一条人工图片决定")
        authority_row = authority_by_id.get(decision["decision_id"])
        if not authority_row or any(str(authority_row.get(key) or "") != decision[key] for key in decision):
            raise V10ImageDeliveryError("人工图片决定与持久权威快照不一致")
        image = images.get(decision["image_id"])
        if image is None:
            raise V10ImageDeliveryError("人工决定只能选择该项目已分析的冻结图片")
        if image["image_sha256"] in image_shas:
            raise V10ImageDeliveryError("N 镜必须对应 N 个不同 image_sha256 的图片")
        result[decision["shot_no"]] = {**decision, **image, "decision_snapshot_ref": snapshot_ref, "decision_snapshot_sha256": snapshot_sha}
        image_shas.add(image["image_sha256"])
    if set(result) != {str(row["镜号"]) for row in script_rows}:
        raise V10ImageDeliveryError("N 镜必须对应 N 条人工图片决定与 N 张图片")
    return result


def _resolve_quality_values(
    resolver: object,
    auth_token: str,
    context: Mapping[str, str],
    artifact_resolver: ArtifactResolver,
    quality_artifact_reader: VersionQualityArtifactWriter,
    *,
    shot_count: int,
) -> dict[str, str]:
    _require_object(resolver, "质量门必须来自 T07 VersionQualityResolver，不能传入字典")
    resolve = getattr(resolver, "resolve_authoritative_evidence", None)
    if not callable(resolve) or not str(auth_token or "").strip():
        raise V10ImageDeliveryError("最终交付需要 T07 VersionQualityResolver 与鉴权 token")
    try:
        quality = resolve(
            str(auth_token), project_id=context["project_id"], version_id=context["version_id"],
            expected_project_lock_version=int(context["project_lock_version"]),
        )
    except Exception as exc:
        raise V10ImageDeliveryError("T07 VersionQualityResolver 未提供可用的持久质量证据") from exc
    return _quality_values(
        quality, context, artifact_resolver, quality_artifact_reader,
        shot_count=shot_count,
    )


def _quality_values(
    quality: object,
    context: Mapping[str, str],
    artifact_resolver: ArtifactResolver,
    quality_artifact_reader: VersionQualityArtifactWriter,
    *,
    shot_count: int,
) -> dict[str, str]:
    _require_object(quality, "质量门必须来自 VersionQualityService，不接受调用方字典")
    if _field(quality, "schema_version") != "mplus-version-quality-evidence/v1":
        raise V10ImageDeliveryError("只接受 mplus-version-quality-evidence/v1")
    _require_bound(quality, context, "VersionQualityEvidence")
    machine, review, confirmation = (_field(quality, name) for name in ("machine_audit", "shot_review", "final_confirmation"))
    values = {
        "machine_audit_ref": _required_text(machine, "ref", "machine_audit"),
        "machine_audit_sha256": _required_text(machine, "sha256", "machine_audit"),
        "machine_audit_status": _required_text(machine, "status", "machine_audit"),
        "shot_snapshot_ref": _required_text(review, "snapshot_ref", "shot_review"),
        "shot_snapshot_sha256": _required_text(review, "snapshot_sha256", "shot_review"),
        "confirmation_id": _required_text(confirmation, "confirmation_id", "final_confirmation"),
        "final_decision": _required_text(confirmation, "decision", "final_confirmation"),
        "confirmation_evidence_ref": _required_text(confirmation, "evidence_ref", "final_confirmation"),
        "confirmation_evidence_sha256": _required_text(confirmation, "evidence_sha256", "final_confirmation"),
        "confirmation_reviewer_actor_id": _required_text(confirmation, "reviewer_actor_id", "final_confirmation"),
        "confirmed_at": _required_text(confirmation, "confirmed_at", "final_confirmation"),
    }
    for ref, sha, label in (
        (values["machine_audit_ref"], values["machine_audit_sha256"], "machine_audit"),
        (values["shot_snapshot_ref"], values["shot_snapshot_sha256"], "shot_review"),
        (values["confirmation_evidence_ref"], values["confirmation_evidence_sha256"], "final_confirmation"),
    ):
        _require_artifact(ref, sha, label)
    _require_quality_artifact(
        values["shot_snapshot_ref"], values["shot_snapshot_sha256"], "shot-review"
    )
    _require_quality_artifact(
        values["confirmation_evidence_ref"], values["confirmation_evidence_sha256"],
        "final-confirmation",
    )
    if values["machine_audit_ref"] != f"artifact://run/{context['run_id']}/run_report":
        raise V10ImageDeliveryError("machine_audit 必须绑定当前 run 的 run_report artifact")
    machine_payload = _json_object(
        _resolved_artifact(artifact_resolver, values["machine_audit_ref"], values["machine_audit_sha256"]),
        "machine_audit run_report",
    )
    if (
        str(machine_payload.get("run_id") or "") != context["run_id"]
        or str(machine_payload.get("machine_audit_status") or "") != values["machine_audit_status"]
    ):
        raise V10ImageDeliveryError("machine_audit artifact 与 VersionQualityEvidence 不一致")
    review_payload = _quality_artifact_json(
        quality_artifact_reader, values["shot_snapshot_ref"], values["shot_snapshot_sha256"],
        "shot_review snapshot",
    )
    final_payload = _quality_artifact_json(
        quality_artifact_reader, values["confirmation_evidence_ref"], values["confirmation_evidence_sha256"],
        "final_confirmation evidence",
    )
    try:
        counts = tuple(int(_field(review, name)) for name in ("total_count", "resolved_count", "unresolved_count"))
    except (TypeError, ValueError) as exc:
        raise V10ImageDeliveryError("shot_review 计数无效") from exc
    if values["machine_audit_status"] != "pass" or counts != (shot_count, shot_count, 0):
        raise V10ImageDeliveryError("最终 Excel 要求 machine pass 与 N 镜全部处理")
    if values["final_decision"] != "quality_pass":
        raise V10ImageDeliveryError("最终 Excel 要求独立人工整片确认 quality_pass")
    expected_review = {
        "workspace_id": context["workspace_id"], "project_id": context["project_id"],
        "version_id": context["version_id"], "run_id": context["run_id"], "total_count": counts[0],
        "resolved_count": counts[1], "unresolved_count": counts[2],
    }
    if any(str(review_payload.get(name)) != str(expected) for name, expected in expected_review.items()):
        raise V10ImageDeliveryError("shot_review 持久 artifact 与 VersionQualityEvidence 不一致")
    expected_final = {
        "workspace_id": context["workspace_id"], "project_id": context["project_id"],
        "version_id": context["version_id"], "run_id": context["run_id"],
        "snapshot_ref": values["shot_snapshot_ref"], "snapshot_sha256": values["shot_snapshot_sha256"],
    }
    confirmation_payload = final_payload.get("confirmation")
    expected_confirmation = {
        "confirmation_id": values["confirmation_id"], "decision": values["final_decision"],
        "reviewer_actor_id": values["confirmation_reviewer_actor_id"],
        "confirmed_at": values["confirmed_at"],
    }
    if (
        final_payload.get("schema_version") != "mplus-final-confirmation-evidence/v1"
        or any(str(final_payload.get(name) or "") != expected for name, expected in expected_final.items())
        or not isinstance(confirmation_payload, Mapping)
        or any(str(confirmation_payload.get(name) or "") != expected for name, expected in expected_confirmation.items())
    ):
        raise V10ImageDeliveryError("final_confirmation 持久 artifact 与 VersionQualityEvidence 不一致")
    values["quality_evidence_sha256"] = hashlib.sha256(json.dumps({
        "machine_audit_sha256": values["machine_audit_sha256"],
        "shot_snapshot_sha256": values["shot_snapshot_sha256"],
        "confirmation_evidence_sha256": values["confirmation_evidence_sha256"],
        "confirmation_id": values["confirmation_id"],
        "reviewer_actor_id": values["confirmation_reviewer_actor_id"],
        "confirmed_at": values["confirmed_at"],
    }, sort_keys=True).encode("utf-8")).hexdigest()
    return values


def _script_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_rows = payload.get("script_rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise V10ImageDeliveryError("visual_binding 缺少非空 script_rows")
    result, shots = [], set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise V10ImageDeliveryError("visual_binding script_rows 含无效记录")
        row, shot = dict(raw), str(raw.get("镜号") or "").strip()
        if not shot or shot in shots:
            raise V10ImageDeliveryError("visual_binding 脚本镜号必须唯一且非空")
        if not str(row.get("画面描述") or "").strip() or not str(row.get("声音") or "").strip() or row.get("时长(秒)") in (None, ""):
            raise V10ImageDeliveryError(f"镜头 {shot} 缺少画面、声音或时长")
        row["镜号"] = shot
        result.append(row)
        shots.add(shot)
    return result


def _binding_by_shot(payload: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    raw_bindings = payload.get("bindings")
    if not isinstance(raw_bindings, list):
        raise V10ImageDeliveryError("visual_binding 缺少 bindings")
    result = {}
    for raw in raw_bindings:
        if not isinstance(raw, Mapping):
            raise V10ImageDeliveryError("visual_binding bindings 含无效记录")
        shot = str(raw.get("shot_no") or "").strip()
        if not shot or shot in result:
            raise V10ImageDeliveryError("visual_binding 每镜必须且只能有一条绑定")
        result[shot] = dict(raw)
    if set(result) != {str(row["镜号"]) for row in rows}:
        raise V10ImageDeliveryError("visual_binding bindings 必须与脚本镜号一一对应")
    return result


def _require_child_version_for_replacements(values: Mapping[str, str], prior: Mapping[str, Mapping[str, Any]], decisions: Mapping[str, Mapping[str, str]]) -> None:
    changed = any(str(prior[shot].get("image_id") or "") and prior[shot].get("image_id") != decision["image_id"] for shot, decision in decisions.items())
    if changed and (not values["parent_version_id"] or values["parent_version_id"] == values["version_id"]):
        raise V10ImageDeliveryError("换图必须由 T00 先创建新子版本后再交付")


def _delivery_rows(values: Mapping[str, str], script_rows: Sequence[Mapping[str, Any]], decisions: Mapping[str, Mapping[str, str]]):
    rows, details, visual_rows, audits, bindings, local_paths = [], [], [], [], [], set()
    for index, script in enumerate(script_rows, start=1):
        decision = decisions[str(script["镜号"])]
        path = decision["controlled_path"]
        rows.append({**dict(script), "画面示意": path, "效果示意": ""})
        local_paths.add(Path(path))
        binding = {key: decision[key] for key in (
            "shot_no", "image_id", "image_sha256", "source_document_id", "source_sheet", "source_row",
            "analysis_ref", "analysis_sha256", "actor_id", "decided_at", "decision_id", "decision_snapshot_ref", "decision_snapshot_sha256",
        )}
        bindings.append(binding)
        details.append({"项目名称": values["project_id"], "slot_index": index, "时长(秒)": script["时长(秒)"], "采用来源类型": "project_frozen_or_supplement", "采用来源文件": decision["source_document_id"], "采用来源 sheet": decision["source_sheet"], "采用来源行": decision["source_row"], "视觉资产ID": decision["image_id"], "image_id": decision["image_id"], "图片绑定方式": "权威人工逐镜决定", "图片绑定说明": "逐镜恰好一张已确认冻结图片", "视觉读取状态": "pass", "配图置信度": "人工确认", "视觉内容摘要": f"analysis_sha256={decision['analysis_sha256']}", "配图判断": "人工确认"})
        visual_rows.append({"visual_asset_id": decision["image_id"], "image_id": decision["image_id"], "slot_index": index, "镜号": script["镜号"], "图片角色": "逐镜确认图", "来源文件": decision["source_document_id"], "来源 sheet": decision["source_sheet"], "来源行": decision["source_row"], "绑定方式": "人工确认", "视觉读取状态": "pass", "配图置信度": "人工确认", "配图判断": "人工确认", "质量指标": f"image_sha256={decision['image_sha256']}; analysis_sha256={decision['analysis_sha256']}", "审核结果": "通过"})
        audits.append({"检查项": "v10_image_decision", "级别": "hard", "状态": "pass", "关联镜号": script["镜号"], "详情": f"image_sha256={decision['image_sha256']}; actor={decision['actor_id']}; at={decision['decided_at']}", "修复建议": ""})
    return rows, details, visual_rows, audits, bindings, local_paths


def _append_delivery_audit(path: Path, values: Mapping[str, str], quality: Mapping[str, str], bindings: Sequence[Mapping[str, str]]) -> None:
    workbook = load_workbook(path)
    try:
        sheet = workbook.create_sheet("交付审核")
        headers = ("workspace_id", "project_id", "version_id", "parent_version_id", "run_id", "visual_binding_ref", "visual_binding_sha256", "quality_evidence_sha256", "machine_audit_ref", "machine_audit_sha256", "shot_review_ref", "shot_review_sha256", "整片确认ID", "整片确认结论", "整片确认ref", "整片确认SHA256", "整片确认审核人", "整片确认时间", "镜号", "图片ID", "图片SHA256", "来源document_id", "来源sheet", "来源行", "图片分析ref", "视觉分析SHA256", "人工", "人工决定时间", "人工决定ID", "人工决定快照ref", "人工决定快照SHA256")
        sheet.append(headers)
        for item in bindings:
            sheet.append((values["workspace_id"], values["project_id"], values["version_id"], values["parent_version_id"], values["run_id"], values["visual_binding_ref"], values["visual_binding_sha256"], quality["quality_evidence_sha256"], quality["machine_audit_ref"], quality["machine_audit_sha256"], quality["shot_snapshot_ref"], quality["shot_snapshot_sha256"], quality["confirmation_id"], quality["final_decision"], quality["confirmation_evidence_ref"], quality["confirmation_evidence_sha256"], quality["confirmation_reviewer_actor_id"], quality["confirmed_at"], item["shot_no"], item["image_id"], item["image_sha256"], item["source_document_id"], item["source_sheet"], item["source_row"], item["analysis_ref"], item["analysis_sha256"], item["actor_id"], item["decided_at"], item["decision_id"], item["decision_snapshot_ref"], item["decision_snapshot_sha256"]))
        sheet.freeze_panes = "A2"
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = min(max(12, max(len(str(cell.value or "")) for cell in column) + 2), 72)
        workbook.save(path)
    finally:
        workbook.close()


def _replace_main_script_sheet(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    workbook = load_workbook(path)
    try:
        old = workbook["脚本"]
        workbook.remove(old)
        sheet = workbook.create_sheet("脚本", 0)
        headers = ("镜号", "结构", "时长(秒)", "画面描述", "声音", "旁白", "字幕", "备注", "画面示意")
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(name="Arial", bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        for index, row in enumerate(rows, start=2):
            sheet.append([row.get(field, "") for field in headers])
            sheet.row_dimensions[index].height = 210
            image = ExcelImage(str(row["画面示意"]))
            image.width, image.height = 320, 180
            sheet.add_image(image, f"I{index}")
        widths = {"A": 10, "B": 16, "C": 11, "D": 42, "E": 24, "F": 28, "G": 28, "H": 22, "I": 56}
        for column, width in widths.items():
            sheet.column_dimensions[column].width = width
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.freeze_panes = "A2"
        workbook.save(path)
    finally:
        workbook.close()


def _verify_reopenable_workbook(path: Path, values: Mapping[str, str], quality: Mapping[str, str], bindings: Sequence[Mapping[str, str]], script_rows: Sequence[Mapping[str, Any]], *, expected_images: int, forbidden_paths: set[Path]) -> None:
    workbook = load_workbook(path, read_only=False, data_only=True)
    try:
        if "脚本" not in workbook.sheetnames or "抽调明细" not in workbook.sheetnames or "交付审核" not in workbook.sheetnames:
            raise V10ImageDeliveryError("交付 Excel 缺少必要工作表")
        script, detail, audit = workbook["脚本"], workbook["抽调明细"], workbook["交付审核"]
        if script.max_row != expected_images + 1 or len(script._images) != expected_images:
            raise V10ImageDeliveryError("交付 Excel 镜头行或内嵌图片数与 N 不一致")
        embedded_shas = {
            image.anchor._from.row + 1: hashlib.sha256(image._data()).hexdigest()
            for image in script._images
        }
        if len(set(embedded_shas.values())) != expected_images:
            raise V10ImageDeliveryError("交付 Excel 的 N 张内嵌图片必须具有不同 image_sha256")
        script_headers = _headers(script)
        detail_headers, audit_headers = _headers(detail), _headers(audit)
        required_audit = {"workspace_id", "project_id", "version_id", "run_id", "visual_binding_ref", "visual_binding_sha256", "quality_evidence_sha256", "machine_audit_sha256", "shot_review_sha256", "整片确认SHA256", "整片确认审核人", "整片确认时间", "图片SHA256", "来源document_id", "来源sheet", "来源行", "图片分析ref", "视觉分析SHA256", "人工", "人工决定时间", "人工决定ID"}
        if required_audit - set(audit_headers):
            raise V10ImageDeliveryError("交付 Excel 缺少版本、来源或人工审核字段")
        if detail.max_row != expected_images + 1 or audit.max_row != expected_images + 1:
            raise V10ImageDeliveryError("交付 Excel 逐行审核证据不完整")
        for index, (binding, expected_script) in enumerate(zip(bindings, script_rows), start=2):
            if embedded_shas.get(index) != binding["image_sha256"]:
                raise V10ImageDeliveryError("交付 Excel 镜头内嵌图片与人工决定 image_sha256 不一致")
            if any(not script.cell(index, script_headers[field]).value for field in ("镜号", "画面描述", "声音", "时长(秒)")):
                raise V10ImageDeliveryError("交付 Excel 脚本文字不完整")
            for field in ("镜号", "画面描述", "声音", "时长(秒)", "旁白", "字幕"):
                if str(script.cell(index, script_headers[field]).value or "") != str(expected_script.get(field) or ""):
                    raise V10ImageDeliveryError("交付 Excel 主脚本未保留 visual_binding 字段")
            if detail.cell(index, detail_headers["时长(秒)"]).value in (None, "") or any(not detail.cell(index, detail_headers[field]).value for field in ("采用来源文件", "采用来源 sheet", "采用来源行")):
                raise V10ImageDeliveryError("交付 Excel 来源或时长不完整")
            for key, expected in (("workspace_id", values["workspace_id"]), ("project_id", values["project_id"]), ("version_id", values["version_id"]), ("run_id", values["run_id"]), ("visual_binding_ref", values["visual_binding_ref"]), ("visual_binding_sha256", values["visual_binding_sha256"]), ("quality_evidence_sha256", quality["quality_evidence_sha256"]), ("整片确认审核人", quality["confirmation_reviewer_actor_id"]), ("整片确认时间", quality["confirmed_at"]), ("镜号", binding["shot_no"]), ("图片SHA256", binding["image_sha256"]), ("来源document_id", binding["source_document_id"]), ("来源sheet", binding["source_sheet"]), ("来源行", binding["source_row"]), ("人工", binding["actor_id"]), ("人工决定时间", binding["decided_at"]), ("人工决定ID", binding["decision_id"])):
                if str(audit.cell(index, audit_headers[key]).value or "") != expected:
                    raise V10ImageDeliveryError("交付 Excel 逐行版本、来源或人工证据不一致")
    finally:
        workbook.close()
    _assert_archive_has_no_paths(path, forbidden_paths)


def _pdf_pages_from_excel(excel: V10ImageDeliveryArtifact) -> list[dict[str, Any]]:
    workbook = load_workbook(excel.workbook_path, read_only=False, data_only=True)
    try:
        script, audit = workbook["脚本"], workbook["交付审核"]
        script_headers, audit_headers = _headers(script), _headers(audit)
        images = {image.anchor._from.row + 1: image._data() for image in script._images}
        pages = []
        for row_index in range(2, script.max_row + 1):
            audit_row = {key: audit.cell(row_index, column).value for key, column in audit_headers.items()}
            if (str(audit_row["version_id"] or "") != excel.version_id or str(audit_row["run_id"] or "") != excel.run_id or str(audit_row["visual_binding_sha256"] or "") != excel.visual_binding_sha256 or str(audit_row["quality_evidence_sha256"] or "") != excel.quality_evidence_sha256 or str(audit_row["整片确认审核人"] or "") != excel.final_reviewer_actor_id or str(audit_row["整片确认时间"] or "") != excel.final_confirmed_at):
                raise V10ImageDeliveryError("Excel 与请求 PDF 的 version/run/SHA 不一致")
            image_data = images.get(row_index)
            if not image_data:
                raise V10ImageDeliveryError("Excel 缺少可供 PDF 使用的逐镜图片")
            pages.append({"shot_no": str(script.cell(row_index, script_headers["镜号"]).value), "duration": str(script.cell(row_index, script_headers["时长(秒)"]).value), "visual": str(script.cell(row_index, script_headers["画面描述"]).value), "sound": str(script.cell(row_index, script_headers["声音"]).value), "narration": str(script.cell(row_index, script_headers["旁白"]).value or ""), "subtitle": str(script.cell(row_index, script_headers["字幕"]).value or ""), "image": image_data, "image_sha256": str(audit_row["图片SHA256"]), "source": f"{audit_row['来源document_id']} / {audit_row['来源sheet']} / {audit_row['来源行']}"})
        return pages
    finally:
        workbook.close()


def _write_pdf(path: Path, excel: V10ImageDeliveryArtifact, pages: Sequence[Mapping[str, Any]]) -> int:
    canvas = Canvas(str(path), pagesize=A4)
    canvas.setTitle("Loomtake V10 image script delivery")
    canvas.setSubject(f"project={excel.project_id};version={excel.version_id};run={excel.run_id};visual_binding_sha256={excel.visual_binding_sha256};quality_evidence_sha256={excel.quality_evidence_sha256};final_reviewer_actor_id={excel.final_reviewer_actor_id};final_confirmed_at={excel.final_confirmed_at};excel_sha256={excel.workbook_sha256}")
    page_count = _render_pdf_pages(canvas, pages, title="Loomtake 同版图片脚本交付", metadata=(
        f"version={excel.version_id}  run={excel.run_id}",
        f"excel_sha256={excel.workbook_sha256}",
        f"visual_binding_sha256={excel.visual_binding_sha256}",
        f"quality_evidence_sha256={excel.quality_evidence_sha256}",
        f"final_reviewer={excel.final_reviewer_actor_id}  confirmed_at={excel.final_confirmed_at}",
    ), image_bottom=A4[1] - 400)
    canvas.save()
    return page_count


def _render_pdf_pages(canvas: Canvas, pages: Sequence[Mapping[str, Any]], *, title: str, metadata: Sequence[str], image_bottom: float) -> int:
    font_name = _pdf_font()
    width, height = A4
    page_count = 0
    for number, page in enumerate(pages, start=1):
        def start_page(continuation: bool) -> float:
            nonlocal page_count
            page_count += 1
            canvas.setFont(font_name, 12)
            suffix = "  续页" if continuation else ""
            canvas.drawString(40, height - 42, f"{title}  镜头 {page['shot_no']}  ({number}/{len(pages)}){suffix}")
            canvas.setFont("Helvetica", 6)
            for index, line in enumerate(metadata):
                canvas.drawString(40, height - 58 - 11 * index, line)
            canvas.setFont("Helvetica", 8)
            canvas.drawString(40, 34, f"shot={page['shot_no']} duration={page['duration']} image_sha256={page['image_sha256']}")
            if not continuation:
                canvas.drawImage(ImageReader(BytesIO(page["image"])), 40, image_bottom, width=515, height=260, preserveAspectRatio=True, anchor="c")
            canvas.setFont(font_name, 10)
            return height - 58 - 11 * len(metadata) - 24 if continuation else image_bottom - 24

        cursor = start_page(False)
        for label, key in (("画面", "visual"), ("声音", "sound"), ("旁白", "narration"), ("字幕", "subtitle"), ("改编依据", "adaptation"), ("来源", "source")):
            if key == "adaptation" and not page.get(key):
                continue
            for line in _wrapped_pdf_lines(canvas, f"{label}：{page[key]}", width - 80, font_name):
                if cursor < 62:
                    canvas.showPage()
                    cursor = start_page(True)
                canvas.drawString(40, cursor, line)
                cursor -= 16
            cursor -= 16
        canvas.showPage()
    return page_count


def _wrapped_pdf_lines(canvas: Canvas, text: str, width: float, font_name: str):
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = ""
        for char in paragraph:
            if line and canvas.stringWidth(line + char, font_name, 10) > width:
                yield line
                line = char
            else:
                line += char
        yield line


def _pdf_font() -> str:
    name = "MPlusCJK"
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    candidates = (
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    )
    font_path = next((item for item in candidates if item.is_file()), None)
    if font_path is None:
        raise V10ImageDeliveryError("缺少可嵌入的 CJK TrueType 字体，不能生成可读 PDF")
    pdfmetrics.registerFont(TTFont(name, str(font_path), subfontIndex=0))
    return name


def _verify_pdf(path: Path, excel: V10ImageDeliveryArtifact, *, expected_pages: int, forbidden_paths: set[Path]) -> None:
    reader = PdfReader(str(path))
    if len(reader.pages) != expected_pages:
        raise V10ImageDeliveryError("同版 PDF 页数与排版结果不一致")
    subject = str((reader.metadata or {}).get("/Subject") or "")
    if any(token not in subject for token in (f"version={excel.version_id}", f"run={excel.run_id}", f"visual_binding_sha256={excel.visual_binding_sha256}", f"quality_evidence_sha256={excel.quality_evidence_sha256}", f"final_reviewer_actor_id={excel.final_reviewer_actor_id}", f"final_confirmed_at={excel.final_confirmed_at}", f"excel_sha256={excel.workbook_sha256}")):
        raise V10ImageDeliveryError("PDF 缺少同版 Excel 的 version/run/SHA 对照")
    for page in reader.pages:
        if "image_sha256=" not in (page.extract_text() or ""):
            raise V10ImageDeliveryError("PDF 缺少逐页图片 SHA 抽查字段")
    raw = path.read_bytes()
    if b".mplus" in raw or any(str(item.resolve()).encode() in raw for item in forbidden_paths):
        raise V10ImageDeliveryError("PDF 含本地绝对路径泄露")


def _headers(sheet) -> dict[str, int]:
    return {str(cell.value or ""): cell.column for cell in sheet[1]}


def _scrub_runtime_paths(path: Path, image_paths: set[Path], output_root: Path) -> None:
    workbook = load_workbook(path)
    try:
        sensitive = {str(item.resolve()) for item in image_paths | {output_root.resolve()}}
        for sheet in workbook.worksheets:
            columns = {cell.column for cell in sheet[1] if "路径" in str(cell.value or "") or str(cell.value or "") in {"画面示意", "效果示意", "预览图"}}
            for row in sheet.iter_rows(min_row=2):
                for cell in row:
                    value = str(cell.value or "")
                    if cell.column in columns or value in sensitive or value.startswith("file://") or ".mplus" in value:
                        cell.value, cell.hyperlink = None, None
                    elif cell.hyperlink:
                        target = str(cell.hyperlink.target or "")
                        if target in sensitive or target.casefold().startswith(("file://", "/", "~/")) or ".mplus" in target.casefold():
                            cell.hyperlink = None
        workbook.save(path)
    finally:
        workbook.close()


def _assert_archive_has_no_paths(path: Path, forbidden_paths: set[Path]) -> None:
    tokens = [str(item.resolve()).encode() for item in forbidden_paths] + [b".mplus"]
    with ZipFile(path) as archive:
        for name in archive.namelist():
            data = archive.read(name)
            if any(token in data for token in tokens):
                raise V10ImageDeliveryError("交付 Excel 含本地绝对路径泄露")


def _resolved_artifact(resolver: ArtifactResolver, ref: str, expected_sha256: str) -> Path:
    try:
        path = Path(resolver(ref, expected_sha256)).resolve()
    except Exception as exc:
        raise V10ImageDeliveryError("无法解析受控 artifact 引用") from exc
    if not path.is_file() or _sha256_file(path) != expected_sha256:
        raise V10ImageDeliveryError("artifact 文件不存在或 SHA 已漂移")
    return path


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V10ImageDeliveryError(f"{label} 不是可读取 JSON") from exc
    if not isinstance(value, dict):
        raise V10ImageDeliveryError(f"{label} 必须为 JSON 对象")
    return value


def _quality_artifact_json(
    reader: VersionQualityArtifactWriter, ref: str, sha256: str, label: str,
) -> dict[str, Any]:
    _require_object(reader, "质量 artifact 必须由 T07 VersionQualityArtifactWriter 读取")
    read_json = getattr(reader, "read_json", None)
    if not callable(read_json):
        raise V10ImageDeliveryError("质量 artifact 必须由 T07 VersionQualityArtifactWriter 读取")
    try:
        payload = read_json(ref, sha256)
    except Exception as exc:
        raise V10ImageDeliveryError(f"{label} SHA 已漂移或不可由 T07 持久 artifact 读取") from exc
    if not isinstance(payload, dict):
        raise V10ImageDeliveryError(f"{label} 根必须是 object")
    return payload


def _require_artifact(ref: str, sha: str, label: str) -> None:
    if not ref.startswith("artifact://") or not _is_sha256(sha):
        raise V10ImageDeliveryError(f"{label} 必须提供 artifact:// ref 与 SHA-256")


def _require_quality_artifact(ref: str, sha: str, kind: str) -> None:
    if ref != f"artifact://version-quality/{kind}/{sha}":
        raise V10ImageDeliveryError(f"{kind} 必须为 T07 content-addressed 持久 artifact")


def _require_bound(value: object, context: Mapping[str, str], label: str) -> None:
    for name in ("workspace_id", "project_id", "version_id", "run_id"):
        if _required_text(value, name, label) != context[name]:
            raise V10ImageDeliveryError(f"{label} 的 {name} 与交付版本不一致")


def _require_object(value: object, message: str) -> None:
    if isinstance(value, Mapping) or value is None:
        raise V10ImageDeliveryError(message)


def _field(value: object, name: str) -> object:
    return getattr(value, name, "")


def _required_text(value: object, name: str, label: str) -> str:
    result = str(_field(value, name) or "").strip()
    if not result:
        raise V10ImageDeliveryError(f"{label} 缺少 {name}")
    return result


def _delivery_root(output_root: str | Path) -> Path:
    root = Path(output_root).expanduser().resolve()
    if ".mplus" in root.parts or root.name == "CURRENT":
        raise V10ImageDeliveryError("不得写入真实 ~/.mplus 或 CURRENT")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _require_clear_non_solid_image(path: Path) -> None:
    try:
        with PILImage.open(path) as image:
            image.load()
            width, height = image.size
            extrema = image.convert("RGB").getextrema()
    except Exception as exc:
        raise V10ImageDeliveryError(f"图片无法独立读取: {path.name}") from exc
    if width < 320 or height < 180:
        raise V10ImageDeliveryError(f"图片不满足清晰度下限: {path.name}")
    if all(low == high for low, high in extrema):
        raise V10ImageDeliveryError(f"禁止纯色占位图: {path.name}")


def _snapshot_sha(values: Mapping[str, str], quality: Mapping[str, str], bindings: Sequence[Mapping[str, str]]) -> str:
    return hashlib.sha256(json.dumps({"project_id": values["project_id"], "version_id": values["version_id"], "run_id": values["run_id"], "visual_binding_sha256": values["visual_binding_sha256"], "machine_audit_sha256": quality["machine_audit_sha256"], "confirmation_evidence_sha256": quality["confirmation_evidence_sha256"], "bindings": list(bindings)}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())
