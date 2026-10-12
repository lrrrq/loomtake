"""Project-bound, isolated Alpha image decision and Excel snapshot flow."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw

from .alpha_brief import AlphaBriefStore
from .alpha_image_delivery import AlphaImageDeliveryError, create_alpha_image_delivery
from .identity_service import LocalIdentityAdapter
from .project_service import ProjectService
from .workspace_store import WorkspaceServiceError, utc_now


class AlphaImageFlow:
    """Keeps Alpha-only candidate and delivery records below its safe temp root."""

    def __init__(
        self,
        *,
        projects: ProjectService,
        identity: LocalIdentityAdapter,
        alpha_briefs: AlphaBriefStore,
    ) -> None:
        self.projects = projects
        self.identity = identity
        self.alpha_briefs = alpha_briefs
        self.root = alpha_briefs.root

    def prepare_images(self, auth_token: str, *, project_id: str, script_rows: list[dict]) -> dict:
        source = self._brief_source(auth_token, project_id)
        rows = _script_rows(script_rows)
        candidates = [self._candidate(project_id, row, source, revision=0) for row in rows]
        self._write_candidates(project_id, candidates)
        return {
            "calls_models": False,
            "provider_calls": 0,
            "fixture": True,
            "notice": "隔离 fixture 占位图仅验证闭环，不构成真实 Provider 视觉质量结论。",
            "candidates": candidates,
            "source": source,
        }

    def replace_image(
        self, auth_token: str, *, project_id: str, shot_no: str, script_rows: list[dict]
    ) -> dict:
        source = self._brief_source(auth_token, project_id)
        rows = _script_rows(script_rows)
        shot = str(shot_no or "").strip()
        row = next((item for item in rows if item["镜号"] == shot), None)
        if row is None:
            raise WorkspaceServiceError("INVALID_INPUT", "换图镜号不属于当前脚本")
        existing = self._read_candidates(project_id)
        revision = 1 + sum(1 for item in existing if item["shot_no"] == shot)
        replacement = self._candidate(project_id, row, source, revision=revision)
        self._write_candidates(project_id, [*existing, replacement])
        return {
            "calls_models": False,
            "provider_calls": 0,
            "fixture": True,
            "candidate": replacement,
        }

    def create_delivery(
        self,
        auth_token: str,
        *,
        project_id: str,
        version_id: str,
        script_rows: list[dict],
        selections: list[dict],
    ) -> dict:
        rows = _script_rows(script_rows)
        version = self.projects.get_project_version(auth_token, version_id)
        if version["project_id"] != project_id:
            raise WorkspaceServiceError("PERMISSION_DENIED", "version 不属于当前项目")
        candidates = {item["candidate_id"]: item for item in self._read_candidates(project_id)}
        chosen = _selections(selections, rows, candidates)
        self._verify_candidates(auth_token, project_id, chosen)
        actor_id = str(self.identity.resolve_auth_session(auth_token)["actor_id"])
        confirmed_at = utc_now()
        decisions = [{
            "shot_no": item["shot_no"],
            "image_id": item["candidate_id"],
            "image_path": item["image_path"],
            "confirmed_by": actor_id,
            "confirmed_at": confirmed_at,
            "source_document_id": item["source_document_id"],
            "source_sheet": item["source_sheet"],
            "source_row": item["source_row"],
        } for item in chosen]
        try:
            delivery = create_alpha_image_delivery(
                self.root,
                script_rows=rows,
                image_decisions=decisions,
                run_id=version["run_id"],
                version_id=version_id,
            )
        except AlphaImageDeliveryError as exc:
            raise WorkspaceServiceError("ALPHA_IMAGE_DELIVERY_INVALID", str(exc)) from exc
        record = {
            "delivery_sha256": delivery.workbook_sha256,
            "workbook_name": delivery.workbook_path.name,
            "run_id": delivery.run_id,
            "version_id": delivery.version_id,
            "bindings": delivery.bindings,
            "created_by_actor_id": actor_id,
            "created_at": confirmed_at,
        }
        self._write_once(self._delivery_path(project_id, delivery.workbook_sha256), record)
        return {
            "calls_models": False,
            "provider_calls": 0,
            "fixture": True,
            **record,
            "workbook_ref": f"artifact://alpha-image-delivery/{delivery.workbook_sha256}",
        }

    def workbook_path(self, auth_token: str, *, project_id: str, delivery_sha256: str) -> Path:
        self.projects.get_project(auth_token, project_id)
        record = self._read_json(self._delivery_path(project_id, delivery_sha256))
        path = self.root / str(record.get("workbook_name") or "")
        if not path.is_file() or _sha256(path) != delivery_sha256:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "Alpha Excel 快照不可用或 SHA 已变化")
        return path

    def _brief_source(self, auth_token: str, project_id: str) -> dict:
        confirmed = self._confirmed_artifact(auth_token, project_id)
        locations = self._source_locations(confirmed)
        if not locations:
            raise WorkspaceServiceError(
                "ALPHA_SOURCE_EVIDENCE_UNAVAILABLE", "已确认 Brief 没有可用于图片确认的 sheet/row 来源证据"
            )
        return locations[0]

    def _confirmed_artifact(self, auth_token: str, project_id: str) -> dict:
        snapshot = self.projects.get_current_brief_snapshot(auth_token, project_id)
        if snapshot is None:
            raise WorkspaceServiceError("BRIEF_ARTIFACT_UNAVAILABLE", "项目没有当前 Brief")
        # The normalized snapshot is verified by AlphaProjectFlow before entry;
        # reconstruct the confirmed artifact through the Alpha store here.
        from .brief_artifact_store import BriefArtifactStore
        store = BriefArtifactStore(self.projects.store.database_path.parent)
        normalized = store.read(snapshot["brief_ref"], snapshot["brief_sha256"])
        confirmed = self.alpha_briefs.read_confirmed(
            project_id,
            str(normalized.get("Alpha Brief引用") or ""),
            str(normalized.get("Brief来源 SHA256") or ""),
        )
        return confirmed

    def _verify_candidates(self, auth_token: str, project_id: str, candidates: list[dict]) -> None:
        confirmed = self._confirmed_artifact(auth_token, project_id)
        valid_sources = {
            (item["source_document_id"], item["source_sheet"], item["source_row"])
            for item in self._source_locations(confirmed)
        }
        for candidate in candidates:
            path = Path(str(candidate.get("image_path") or ""))
            expected_sha = str(candidate.get("image_sha256") or "")
            if not path.is_file() or _sha256(path) != expected_sha:
                raise WorkspaceServiceError(
                    "ALPHA_IMAGE_CANDIDATE_MISMATCH", "已选择图片候选已变化、替换或丢失"
                )
            source = (
                str(candidate.get("source_document_id") or ""),
                str(candidate.get("source_sheet") or ""),
                str(candidate.get("source_row") or ""),
            )
            if source not in valid_sources:
                raise WorkspaceServiceError(
                    "ALPHA_SOURCE_EVIDENCE_UNAVAILABLE", "图片来源三元组不再存在于已确认 Brief"
                )

    @staticmethod
    def _source_locations(confirmed: dict) -> list[dict]:
        brief = dict(confirmed.get("brief") or {})
        documents = {
            str(item.get("document_id") or ""): dict(item)
            for item in brief.get("source_documents") or []
        }
        locations_by_ref = {
            str(location.get("evidence_ref") or ""): {
                "source_document_id": str(extraction.get("document_id") or ""),
                "source_sheet": str((location.get("location") or {}).get("sheet") or ""),
                "source_row": str((location.get("location") or {}).get("row") or ""),
            }
            for extraction in brief.get("extractions") or []
            if extraction.get("status") == "succeeded"
            for location in extraction.get("locations") or []
            if isinstance(location, dict)
        }
        preferred_refs = [
            ref
            for field in ("项目名称", "脚本类型", "目标时长", "风格调性", "必须出现的内容")
            for fact in brief.get("facts") or []
            if fact.get("field") == field
            for ref in fact.get("evidence_refs") or []
        ]
        candidates = [locations_by_ref.get(str(ref)) for ref in preferred_refs]
        candidates.extend(locations_by_ref.values())
        result, seen = [], set()
        for item in candidates:
            if not item or not all(item.values()):
                continue
            source = documents.get(item["source_document_id"], {})
            if not str(source.get("filename") or "").lower().endswith(".xlsx"):
                continue
            key = (item["source_document_id"], item["source_sheet"], item["source_row"])
            if key not in seen:
                seen.add(key)
                result.append(item)
        return result

    def _candidate(self, project_id: str, row: dict, source: dict, *, revision: int) -> dict:
        shot = row["镜号"]
        seed = hashlib.sha256(
            json.dumps({"project_id": project_id, "shot": shot, "row": row, "revision": revision}, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        candidate_id = f"alpha_candidate_{seed[:20]}"
        path = self.root / "image-candidates" / f"{candidate_id}.png"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            color = tuple(int(seed[index:index + 2], 16) for index in (0, 2, 4))
            image = Image.new("RGB", (960, 540), color)
            draw = ImageDraw.Draw(image)
            draw.rectangle((32, 32, 928, 508), outline=(255, 255, 255), width=5)
            draw.text((64, 64), f"ALPHA / SHOT {shot} / R{revision}", fill=(255, 255, 255))
            draw.text((64, 112), str(row.get("画面描述") or row.get("结构") or "匿名画面")[:80], fill=(255, 255, 255))
            image.save(path, format="PNG")
        return {
            "candidate_id": candidate_id,
            "shot_no": shot,
            "image_path": str(path),
            "image_sha256": _sha256(path),
            **source,
        }

    def _read_candidates(self, project_id: str) -> list[dict]:
        path = self.root / "image-candidates" / f"{project_id}.json"
        return list(self._read_json(path).get("items") or []) if path.is_file() else []

    def _write_candidates(self, project_id: str, items: list[dict]) -> None:
        path = self.root / "image-candidates" / f"{project_id}.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps({"items": items}, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def _delivery_path(self, project_id: str, digest: str) -> Path:
        if not isinstance(digest, str) or len(digest) != 64:
            raise WorkspaceServiceError("INVALID_INPUT", "Alpha Excel 快照 SHA 无效")
        return self.root / "image-deliveries" / project_id / f"{digest}.json"

    @staticmethod
    def _write_once(path: Path, value: dict) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not path.is_file():
            raise WorkspaceServiceError("NOT_FOUND", "Alpha 图片交付记录不存在")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "Alpha 图片交付记录不可读取") from exc
        if not isinstance(value, dict):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "Alpha 图片交付记录无效")
        return value


def _script_rows(rows: list[dict]) -> list[dict]:
    if not isinstance(rows, list) or not rows:
        raise WorkspaceServiceError("INVALID_INPUT", "script_rows 至少需要一镜")
    result = [dict(item) for item in rows if isinstance(item, dict)]
    shots = [str(item.get("镜号") or "").strip() for item in result]
    if len(result) != len(rows) or not all(shots) or len(set(shots)) != len(shots):
        raise WorkspaceServiceError("INVALID_INPUT", "script_rows 镜号必须唯一且非空")
    for item, shot in zip(result, shots):
        item["镜号"] = shot
    return result


def _selections(selections: list[dict], rows: list[dict], candidates: dict[str, dict]) -> list[dict]:
    if not isinstance(selections, list):
        raise WorkspaceServiceError("INVALID_INPUT", "images 必须是逐镜候选选择")
    selected: dict[str, dict] = {}
    for raw in selections:
        if not isinstance(raw, dict):
            raise WorkspaceServiceError("INVALID_INPUT", "图片选择必须是 object")
        if "confirmed_by" in raw or "confirmed_at" in raw:
            raise WorkspaceServiceError("ALPHA_ACTOR_FORGED", "逐镜图片确认人和时间必须由服务端确定")
        shot, candidate_id = str(raw.get("shot_no") or ""), str(raw.get("candidate_id") or "")
        candidate = candidates.get(candidate_id)
        if not shot or shot in selected or candidate is None or candidate.get("shot_no") != shot:
            raise WorkspaceServiceError("ALPHA_IMAGE_DELIVERY_INVALID", "每镜必须选择同镜的一个已准备图片")
        selected[shot] = candidate
    shots = [item["镜号"] for item in rows]
    if set(selected) != set(shots):
        raise WorkspaceServiceError("ALPHA_IMAGE_DELIVERY_INVALID", "图片选择必须与脚本镜号一一对应")
    return [selected[shot] for shot in shots]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
