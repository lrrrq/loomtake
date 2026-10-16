"""Project-scoped image pool and manual, provider-free analysis authority.

The module deliberately accepts domain instances for analysis input and exposes
separate employee views, so browser callers never supply or receive storage
paths, artifact references, or content hashes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata
import shutil
from io import BytesIO

from PIL import Image

from .identity_service import require_permission
from .image_preview import image_preview_bytes
from .image_understanding import (
    PROJECT_IMAGE_NEGATIVE_FLAGS,
    PROJECT_IMAGE_QUALITY_FLAGS,
    normalize_visual_analysis,
    normalize_visual_flags,
    project_image_pixel_evidence,
)
from .workspace_store import (
    WorkspaceServiceError, WorkspaceStore, new_id, require_artifact_ref,
    require_sha256, utc_now,
)


_IMAGE_STATUSES = frozenset({"pass", "low_confidence", "blocked", "failed"})
_PROVIDER_IMAGE_STATUSES = frozenset({"pass", "low_confidence", "blocked"})
_UTC_Z = re.compile(r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d{1,6}))?Z$")


@dataclass(frozen=True)
class ArtifactEvidence:
    ref: str
    sha256: str

    def __post_init__(self) -> None:
        require_artifact_ref(self.ref, "artifact_ref")
        require_sha256(self.sha256, "artifact_sha256")


@dataclass(frozen=True)
class ManualImageAnalysisInput:
    image_id: str
    image_sha256: str
    brief_version_id: str
    brief_snapshot_id: str
    status: str
    caption: str
    tags: tuple[str, ...]
    subjects: tuple[str, ...]
    scene: str
    purpose: str
    quality: str
    brand: str
    evidence: tuple[str, ...]
    quality_flags: tuple[str, ...] = ()
    negative_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.image_id, str) or not self.image_id:
            raise WorkspaceServiceError("INVALID_INPUT", "image_id 不能为空")
        require_sha256(self.image_sha256, "image_sha256")
        if self.status not in _IMAGE_STATUSES:
            raise WorkspaceServiceError("INVALID_INPUT", "图片分析 status 无效")
        if not all(isinstance(value, str) and value.strip() for value in (*self.tags, *self.subjects, *self.evidence)):
            raise WorkspaceServiceError("INVALID_INPUT", "图片分析列表字段无效")
        if not all(isinstance(value, str) and value.strip() for value in (self.caption, self.scene, self.purpose, self.quality, self.brand)):
            raise WorkspaceServiceError("INVALID_INPUT", "图片分析字段不能为空")
        _validate_visual_flags(self.quality_flags, self.negative_flags)


@dataclass(frozen=True)
class ProviderImageAnalysisInput:
    image_id: str
    image_sha256: str
    brief_version_id: str
    brief_snapshot_id: str
    status: str
    caption: str
    tags: tuple[str, ...]
    subjects: tuple[str, ...]
    scene: str
    purpose: str
    quality: str
    brand: str
    evidence: tuple[str, ...]
    provider: str
    model: str
    prompt_version: str
    quality_flags: tuple[str, ...] = ()
    negative_flags: tuple[str, ...] = ()
    visual_observations: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.image_id, str) or not self.image_id:
            raise WorkspaceServiceError("INVALID_INPUT", "image_id 不能为空")
        require_sha256(self.image_sha256, "image_sha256")
        if self.status not in _PROVIDER_IMAGE_STATUSES:
            raise WorkspaceServiceError("INVALID_INPUT", "自动图片理解 status 无效")
        if any(
            not isinstance(values, tuple)
            or not values
            or not all(isinstance(value, str) and value.strip() for value in values)
            for values in (self.tags, self.subjects, self.evidence)
        ):
            raise WorkspaceServiceError("INVALID_INPUT", "自动图片理解列表字段无效")
        strings = (
            self.caption, self.scene, self.purpose, self.quality, self.brand,
            self.provider, self.model, self.prompt_version,
        )
        if not all(isinstance(value, str) and value.strip() for value in strings):
            raise WorkspaceServiceError("INVALID_INPUT", "自动图片理解字段不能为空")
        _validate_visual_flags(self.quality_flags, self.negative_flags)


@dataclass(frozen=True)
class ProjectImageSourceLocation:
    source_label: str
    page: int | None = None
    sheet: str | None = None
    row: int | None = None
    slide: int | None = None
    image: int | None = None
    source_document_id: str = ""
    column: int | None = None
    paragraph: int | None = None
    part: str | None = None


@dataclass(frozen=True)
class ProjectImageBusinessSummary:
    caption: str
    tags: tuple[str, ...]
    subjects: tuple[str, ...]
    scene: str
    purpose: str
    quality: str
    brand: str
    evidence_status: str
    quality_flags: tuple[str, ...] = ()
    negative_flags: tuple[str, ...] = ()
    pixel_width: int = 0
    pixel_height: int = 0
    perceptual_hash: str = ""
    visual_observations: dict = field(default_factory=dict)
    source_semantic_text: str = ""
    visual_analysis_available: bool | None = None
    evidence_content_sha256: str = ""
    evidence_prompt_version: str = ""


@dataclass(frozen=True)
class ProjectImageAnalysis:
    analysis_version_id: str
    status: str
    evidence: ArtifactEvidence
    created_by_actor_id: str
    created_at: str


@dataclass(frozen=True)
class ProjectImage:
    image_id: str
    image_sha256: str
    source_links: tuple[ProjectImageSourceLocation, ...]
    current_analysis: ProjectImageAnalysis | None
    business_summary: ProjectImageBusinessSummary | None


@dataclass(frozen=True)
class ProjectImagePlanningReadiness:
    workspace_id: str
    project_id: str
    brief_version_id: str
    brief_snapshot_id: str
    total: int
    passed: int
    pending: int
    low_confidence: int
    blocked: int
    failed: int
    images: tuple[ProjectImage, ...]


@dataclass(frozen=True)
class ProjectImageDeliveryAsset:
    image_id: str
    image_sha256: str
    controlled_path: Path
    source_links: tuple[ProjectImageSourceLocation, ...]
    analysis: ProjectImageAnalysis
    business_summary: ProjectImageBusinessSummary


@dataclass(frozen=True)
class ProjectImageDeliveryPool:
    workspace_id: str
    project_id: str
    brief_version_id: str
    brief_snapshot_id: str
    assets: tuple[ProjectImageDeliveryAsset, ...]
    pending_count: int = 0
    low_confidence_count: int = 0
    blocked_count: int = 0
    failed_count: int = 0


@dataclass(frozen=True)
class ProjectImageProviderAsset:
    image_id: str
    image_sha256: str
    controlled_path: Path
    source_links: tuple[ProjectImageSourceLocation, ...]


@dataclass(frozen=True)
class ProjectImageProviderPool:
    workspace_id: str
    project_id: str
    brief_version_id: str
    brief_snapshot_id: str
    assets: tuple[ProjectImageProviderAsset, ...]


class ProjectImageArtifactStore:
    """Reads frozen input media and writes immutable manual-analysis records."""

    def __init__(self, data_root: str | Path):
        self.root = Path(data_root).expanduser().resolve() / "artifacts"

    def read_source_image(self, evidence: ArtifactEvidence) -> bytes:
        expected = f"artifact://formal-image/{evidence.sha256}"
        if evidence.ref != expected:
            raise WorkspaceServiceError("IMAGE_ARTIFACT_DRIFT", "项目图片不是正式冻结输入")
        path = self.root / "formal-images" / evidence.sha256[:2] / evidence.sha256
        return self._read(path, evidence.sha256, "IMAGE_ARTIFACT_DRIFT")

    def stage_analysis(self, value: dict) -> "_AnalysisArtifactBatch":
        raw = _canonical_json(value).encode("utf-8")
        digest = sha256(raw).hexdigest()
        return _AnalysisArtifactBatch(self, raw, digest)

    def stage_feedback(self, value: dict) -> "_FeedbackArtifactBatch":
        raw = _canonical_json(value).encode("utf-8")
        digest = sha256(raw).hexdigest()
        return _FeedbackArtifactBatch(self, raw, digest)

    def stage_source_image(self, source: str | Path, expected_sha256: str) -> "_SourceImageArtifactBatch":
        return _SourceImageArtifactBatch(self, Path(source), expected_sha256)

    def recover_unreferenced_staging(self, referenced_sha256: set[str], *, older_than_seconds: int = 60) -> tuple[str, ...]:
        """Remove stale unreferenced staging and promoted-orphan analysis files."""
        staging = self.root / "project-image-analysis" / ".staging"
        cutoff, removed = time.time() - older_than_seconds, []
        if staging.is_dir():
            for path in staging.rglob("*"):
                if not path.is_file() or path.stat().st_mtime > cutoff:
                    continue
                digest = path.name
                if _analysis_digest(digest) and digest not in referenced_sha256:
                    path.unlink(missing_ok=True)
                    removed.append(digest)
            for path in sorted(staging.rglob("*"), reverse=True):
                if path.is_dir():
                    try:
                        path.rmdir()
                    except OSError:
                        pass
        analysis_root = self.root / "project-image-analysis"
        if analysis_root.is_dir():
            for path in analysis_root.glob("[0-9a-f][0-9a-f]/*"):
                if not path.is_file() or path.stat().st_mtime > cutoff:
                    continue
                digest = path.name
                if _analysis_digest(digest) and path.parent.name == digest[:2] and digest not in referenced_sha256:
                    path.unlink(missing_ok=True)
                    removed.append(digest)
            for path in analysis_root.glob("[0-9a-f][0-9a-f]"):
                try:
                    path.rmdir()
                except OSError:
                    pass
        return tuple(removed)

    def read_analysis(self, evidence: ArtifactEvidence) -> dict:
        if evidence.ref != f"artifact://project-image-analysis/{evidence.sha256}":
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片分析 ArtifactRef/SHA 无效")
        raw = self._read(self.root / "project-image-analysis" / evidence.sha256[:2] / evidence.sha256, evidence.sha256, "IMAGE_ANALYSIS_ARTIFACT_DRIFT")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片分析 artifact 无法解析") from exc
        if not isinstance(value, dict):
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片分析 artifact 不是对象")
        return value

    def read_feedback(self, evidence: ArtifactEvidence) -> dict:
        if evidence.ref != f"artifact://project-image-feedback/{evidence.sha256}":
            raise WorkspaceServiceError("IMAGE_FEEDBACK_EVIDENCE_DRIFT", "图片反馈 ArtifactRef/SHA 无效")
        raw = self._read(
            self.root / "project-image-feedback" / evidence.sha256[:2] / evidence.sha256,
            evidence.sha256,
            "IMAGE_FEEDBACK_EVIDENCE_DRIFT",
        )
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("IMAGE_FEEDBACK_EVIDENCE_DRIFT", "图片反馈 artifact 无法解析") from exc
        if not isinstance(value, dict):
            raise WorkspaceServiceError("IMAGE_FEEDBACK_EVIDENCE_DRIFT", "图片反馈 artifact 不是对象")
        return value

    @staticmethod
    def _read(path: Path, digest: str, code: str) -> bytes:
        if not path.is_file():
            raise WorkspaceServiceError(code, "图片 artifact 缺失")
        value = path.read_bytes()
        if sha256(value).hexdigest() != digest:
            raise WorkspaceServiceError(code, "图片 artifact SHA-256 漂移")
        return value


class _AnalysisArtifactBatch:
    """Staged artifact with explicit compensation for a failed SQL transaction."""

    def __init__(self, store: ProjectImageArtifactStore, raw: bytes, digest: str):
        self.store, self.digest = store, digest
        root = store.root / "project-image-analysis" / ".staging"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="analysis-", dir=root))
        self.staged = self.directory / digest
        self.staged.write_bytes(raw)
        with self.staged.open("rb") as handle:
            os.fsync(handle.fileno())
        if sha256(raw).hexdigest() != digest or sha256(self.staged.read_bytes()).hexdigest() != digest:
            self.rollback()
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "待提交图片分析 SHA-256 无效")
        self.evidence = ArtifactEvidence(f"artifact://project-image-analysis/{digest}", digest)
        self.final_path = store.root / "project-image-analysis" / digest[:2] / digest
        self.created_final = False

    def promote(self) -> ArtifactEvidence:
        self.final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.final_path.exists():
            ProjectImageArtifactStore._read(self.final_path, self.digest, "IMAGE_ANALYSIS_ARTIFACT_DRIFT")
            return self.evidence
        try:
            os.link(self.staged, self.final_path)
            self.created_final = True
        except FileExistsError:
            ProjectImageArtifactStore._read(self.final_path, self.digest, "IMAGE_ANALYSIS_ARTIFACT_DRIFT")
        return self.evidence

    def commit(self) -> None:
        self.staged.unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except OSError:
            pass

    def rollback(self) -> None:
        if self.created_final:
            self.final_path.unlink(missing_ok=True)
        self.staged.unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except OSError:
            pass


class _FeedbackArtifactBatch:
    """Content-addressed evidence for the existing immutable feedback table."""

    def __init__(self, store: ProjectImageArtifactStore, raw: bytes, digest: str):
        self.store, self.digest = store, digest
        root = store.root / "project-image-feedback" / ".staging"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="feedback-", dir=root))
        self.staged = self.directory / digest
        self.staged.write_bytes(raw)
        with self.staged.open("rb") as handle:
            os.fsync(handle.fileno())
        if sha256(raw).hexdigest() != digest or sha256(self.staged.read_bytes()).hexdigest() != digest:
            self.rollback()
            raise WorkspaceServiceError("IMAGE_FEEDBACK_EVIDENCE_DRIFT", "待提交图片反馈 SHA-256 无效")
        self.evidence = ArtifactEvidence(f"artifact://project-image-feedback/{digest}", digest)
        self.final_path = store.root / "project-image-feedback" / digest[:2] / digest
        self.created_final = False

    def promote(self) -> ArtifactEvidence:
        self.final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.final_path.exists():
            ProjectImageArtifactStore._read(
                self.final_path, self.digest, "IMAGE_FEEDBACK_EVIDENCE_DRIFT",
            )
            return self.evidence
        try:
            os.link(self.staged, self.final_path)
            self.created_final = True
        except FileExistsError:
            ProjectImageArtifactStore._read(
                self.final_path, self.digest, "IMAGE_FEEDBACK_EVIDENCE_DRIFT",
            )
        return self.evidence

    def commit(self) -> None:
        self.staged.unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except OSError:
            pass

    def rollback(self) -> None:
        if self.created_final:
            self.final_path.unlink(missing_ok=True)
        self.staged.unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except OSError:
            pass


class _SourceImageArtifactBatch:
    def __init__(self, store: ProjectImageArtifactStore, source: Path, digest: str):
        require_sha256(digest, "image_sha256")
        if not source.is_file() or sha256(source.read_bytes()).hexdigest() != digest:
            raise WorkspaceServiceError("IMAGE_ARTIFACT_DRIFT", "资产包图片缺失或 SHA-256 漂移")
        self.evidence = ArtifactEvidence(f"artifact://formal-image/{digest}", digest)
        self.final_path = store.root / "formal-images" / digest[:2] / digest
        self.created_final = False
        self.source = source

    def promote(self) -> ArtifactEvidence:
        self.final_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.final_path.exists():
            ProjectImageArtifactStore._read(self.final_path, self.evidence.sha256, "IMAGE_ARTIFACT_DRIFT")
            return self.evidence
        try:
            os.link(self.source, self.final_path)
        except OSError:
            shutil.copy2(self.source, self.final_path)
        self.created_final = True
        ProjectImageArtifactStore._read(self.final_path, self.evidence.sha256, "IMAGE_ARTIFACT_DRIFT")
        return self.evidence

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        if self.created_final:
            self.final_path.unlink(missing_ok=True)


class ProjectImagePoolService:
    def __init__(self, store: WorkspaceStore, *, data_root: str | Path):
        self.store = store
        self.store.initialize()
        self.artifacts = ProjectImageArtifactStore(data_root)
        with self.store.connection() as connection:
            rows = connection.execute("SELECT artifact_sha256 FROM project_image_analysis_versions").fetchall()
        self.artifacts.recover_unreferenced_staging({str(row["artifact_sha256"]) for row in rows})

    def current_confirmed_brief_binding(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        expected_project_lock_version: int,
    ) -> tuple[str, str]:
        """Resolve image-upload context before a script exists, using the same Brief gate."""
        with self.store.connection() as connection:
            _project_and_actor(connection, auth_token, workspace_id, project_id, "project.update")
            row = connection.execute(
                """SELECT c.brief_version_id, c.brief_snapshot_id
                   FROM projects p JOIN brief_confirmations c
                     ON c.project_id=p.project_id AND c.workspace_id=p.workspace_id
                    AND c.brief_snapshot_id=p.current_brief_snapshot_id
                   WHERE p.project_id=? AND p.workspace_id=?
                     AND c.confirmation_status='confirmed' AND c.unresolved_conflict_count=0""",
                (project_id, workspace_id),
            ).fetchone()
            if row is None:
                raise WorkspaceServiceError("CONFIRMED_BRIEF_REQUIRED", "请先确认项目资料，再添加图片")
            brief_version_id, snapshot_id = str(row["brief_version_id"]), str(row["brief_snapshot_id"])
            _confirmed_brief_context(connection, workspace_id, project_id, brief_version_id,
                                     snapshot_id, expected_project_lock_version)
            return brief_version_id, snapshot_id
    def pending_source_materialization(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        brief_version_id: str, brief_snapshot_id: str, expected_project_lock_version: int,
    ) -> int:
        """Count confirmed source links not yet adopted; reading never creates them."""
        with self.store.connection() as connection:
            _project_and_actor(connection, auth_token, workspace_id, project_id, "project.read")
            context = _confirmed_brief_context(connection, workspace_id, project_id,
                brief_version_id, brief_snapshot_id, expected_project_lock_version)
            source_ids = _json_list(context["source_document_ids_json"], "source_document_ids_json")
            if not source_ids:
                return 0
            placeholders = ",".join("?" for _ in source_ids)
            return int(connection.execute(
                f"""SELECT COUNT(*) FROM document_images d
                    JOIN project_files f ON f.file_id=d.file_id
                    WHERE f.workspace_id=? AND f.project_id=? AND d.source_document_id IN ({placeholders})
                    AND NOT EXISTS (SELECT 1 FROM project_image_source_links l
                        WHERE l.document_image_id=d.document_image_id)""",
                (workspace_id, project_id, *source_ids),
            ).fetchone()[0])

    def synchronize_confirmed_brief(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        brief_version_id: str, brief_snapshot_id: str, expected_project_lock_version: int,
    ) -> tuple[str, ...]:
        """Materialize source-image rows for one already confirmed Brief, without re-reading client paths."""
        with self.store.transaction() as connection:
            _project_and_actor(connection, auth_token, workspace_id, project_id, "project.update")
            context = _confirmed_brief_context(connection, workspace_id, project_id, brief_version_id, brief_snapshot_id, expected_project_lock_version)
            source_ids = _json_list(context["source_document_ids_json"], "source_document_ids_json")
            if not source_ids:
                return ()
            placeholders = ",".join("?" for _ in source_ids)
            rows = connection.execute(
                f"""SELECT d.document_image_id, d.source_document_id, d.image_sha256,
                           d.artifact_ref, d.content_sha256, d.location_json, f.workspace_id, f.project_id
                    FROM document_images d JOIN project_files f ON f.file_id=d.file_id
                    WHERE f.project_id=? AND d.source_document_id IN ({placeholders})
                    ORDER BY d.document_image_id""",
                (project_id, *source_ids),
            ).fetchall()
            image_ids: list[str] = []
            for row in rows:
                evidence = ArtifactEvidence(str(row["artifact_ref"]), str(row["content_sha256"]))
                if str(row["image_sha256"]) != evidence.sha256:
                    raise WorkspaceServiceError("IMAGE_ARTIFACT_DRIFT", "document_images SHA-256 不一致")
                self.artifacts.read_source_image(evidence)
                image = connection.execute(
                    "SELECT project_image_id FROM project_images WHERE project_id=? AND image_sha256=?",
                    (project_id, evidence.sha256),
                ).fetchone()
                if image is None:
                    image_id = new_id("project_image")
                    connection.execute(
                        """INSERT INTO project_images VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (image_id, workspace_id, project_id, evidence.sha256, evidence.ref, _actor_id(connection, auth_token), utc_now()),
                    )
                else:
                    image_id = str(image["project_image_id"])
                    stored = connection.execute("SELECT artifact_ref FROM project_images WHERE project_image_id=?", (image_id,)).fetchone()
                    if stored is None or stored["artifact_ref"] != evidence.ref:
                        raise WorkspaceServiceError("IMAGE_ARTIFACT_DRIFT", "同 SHA 项目图片 ArtifactRef 不一致")
                exists = connection.execute("SELECT 1 FROM project_image_source_links WHERE document_image_id=?", (row["document_image_id"],)).fetchone()
                if exists is None:
                    connection.execute(
                        """INSERT INTO project_image_source_links VALUES (?, ?, ?, ?, ?, ?)""",
                        (new_id("project_image_source"), image_id, row["document_image_id"], row["source_document_id"], row["location_json"], utc_now()),
                    )
                image_ids.append(image_id)
        return tuple(dict.fromkeys(image_ids))

    def adopt_package_images(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        brief_version_id: str,
        brief_snapshot_id: str,
        expected_project_lock_version: int,
        package_id: str,
        package_version: int,
        candidates: tuple[dict, ...],
    ) -> tuple[str, ...]:
        if not candidates:
            raise WorkspaceServiceError("INVALID_INPUT", "默认图片选择不能为空")
        source_batches = []
        analysis_batches = []
        adopted: list[str] = []
        default_package = (package_id, package_version)
        try:
            with self.store.transaction() as connection:
                _project_and_actor(
                    connection, auth_token, workspace_id, project_id, "review.record",
                )
                _confirmed_brief_context(
                    connection, workspace_id, project_id, brief_version_id,
                    brief_snapshot_id, expected_project_lock_version,
                )
                actor_id = _actor_id(connection, auth_token)
                created_at = utc_now()
                for candidate in candidates:
                    origin = (candidate.get("package_sources") or [{}])[0]
                    package_id = origin.get("package_id", default_package[0])
                    package_version = origin.get("package_version", default_package[1])
                    digest = str(candidate["content_sha256"])
                    source_batch = self.artifacts.stage_source_image(
                        candidate["path"], digest,
                    )
                    source_batches.append(source_batch)
                    evidence = source_batch.promote()
                    existing = connection.execute(
                        "SELECT project_image_id, artifact_ref FROM project_images WHERE project_id=? AND image_sha256=?",
                        (project_id, digest),
                    ).fetchone()
                    if existing is None:
                        image_id = new_id("project_image")
                        connection.execute(
                            "INSERT INTO project_images VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (image_id, workspace_id, project_id, digest, evidence.ref, actor_id, created_at),
                        )
                    else:
                        image_id = str(existing["project_image_id"])
                        if existing["artifact_ref"] != evidence.ref:
                            raise WorkspaceServiceError("IMAGE_ARTIFACT_DRIFT", "项目同 SHA 图片引用漂移")
                    source = dict(candidate["source"])
                    origins = candidate.get('package_sources') or [{'package_id': package_id,
                        'package_version': package_version, 'media_id': candidate['media_id'], 'sources': [source]}]
                    for item in origins:
                        for location in item['sources']:
                            exists = connection.execute(
                                """SELECT 1 FROM project_image_package_source_links
                                WHERE project_image_id=? AND package_id=? AND package_version=? AND media_id=?
                                  AND source_file=? AND source_sheet=? AND source_row=?""",
                                (image_id, item['package_id'], item['package_version'], item['media_id'],
                                 location['file'], location['sheet'], int(location['row'])),
                            ).fetchone()
                            if exists is None:
                                connection.execute(
                                    "INSERT INTO project_image_package_source_links VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (new_id('project_image_package_source'), image_id, item['package_id'],
                                     item['package_version'], item['media_id'], digest, location['file'],
                                     location['sheet'], int(location['row']), actor_id, created_at),
                                )
                    current = connection.execute(
                        """SELECT a.* FROM project_image_analysis_current_links l
                        JOIN project_image_analysis_versions a
                          ON a.project_image_analysis_version_id=l.project_image_analysis_version_id
                        WHERE l.project_image_id=?""",
                        (image_id,),
                    ).fetchone()
                    if current is None or (
                        current["brief_version_id"] != brief_version_id
                        or current["brief_snapshot_id"] != brief_snapshot_id
                    ):
                        analysis_id = new_id("project_image_analysis")
                        observations = _visual_observations(candidate)
                        visual = candidate.get("visual_analysis", candidate)
                        payload = {
                            "schema_version": "mplus-project-image-analysis/v4",
                            "analysis_origin": "source_reference",
                            "workspace_id": workspace_id, "project_id": project_id,
                            "project_image_id": image_id, "image_sha256": digest,
                            "brief_version_id": brief_version_id,
                            "brief_snapshot_id": brief_snapshot_id, "status": "pass",
                            "caption": str(candidate["caption"]),
                            "tags": list(candidate["tags"] or ["类别默认图"]),
                            "subjects": list(candidate["subjects"] or ["类别素材"]),
                            "scene": str(candidate["scene"] or "类别资产包参考场景"),
                            "purpose": "作为当前项目逐镜默认图片候选",
                            "quality": str(candidate["quality"] or "图片质量待核对"),
                            "brand": str(candidate["brand"] or "类别资产包"),
                            "evidence": [
                                f"package:{package_id}@{package_version}",
                                f"media:{candidate['media_id']}",
                                f"source:{source['file']}:{source['sheet']}:{source['row']}",
                            ],
                            "quality_flags": [item for item in normalize_visual_flags(candidate.get("quality_flags")) if item in PROJECT_IMAGE_QUALITY_FLAGS],
                            "negative_flags": [item for item in normalize_visual_flags(candidate.get("negative_flags")) if item in PROJECT_IMAGE_NEGATIVE_FLAGS],
                            **project_image_pixel_evidence(candidate["path"]),
                            "visual_observations": observations,
                            "source_semantic_text": str(candidate.get("source_semantic_text") or ""),
                            "visual_analysis_available": bool(visual.get("caption") and visual.get("tags") and visual.get("vision_recognition_status") != "failed"),
                            "package_id": package_id, "package_version": package_version,
                            "package_sources": candidate.get("package_sources") or [],
                            "media_id": str(candidate["media_id"]),
                            "selected_by_actor_id": actor_id, "selected_at": created_at,
                        }
                        analysis_batch = self.artifacts.stage_analysis(payload)
                        analysis_batches.append(analysis_batch)
                        analysis_evidence = analysis_batch.promote()
                        connection.execute(
                            "INSERT INTO project_image_analysis_versions VALUES (?, ?, ?, ?, ?, ?, ?, 'pass', ?, ?, ?, ?)",
                            (
                                analysis_id, image_id, workspace_id, project_id,
                                brief_version_id, brief_snapshot_id, digest,
                                analysis_evidence.ref, analysis_evidence.sha256,
                                actor_id, created_at,
                            ),
                        )
                        if current is None:
                            connection.execute(
                                "INSERT INTO project_image_analysis_current_links VALUES (?, ?, ?)",
                                (image_id, analysis_id, created_at),
                            )
                        else:
                            connection.execute(
                                """UPDATE project_image_analysis_current_links
                                SET project_image_analysis_version_id=?, updated_at=?
                                WHERE project_image_id=?""",
                                (analysis_id, created_at, image_id),
                            )
                    elif candidate.get('package_sources'):
                        payload = self.artifacts.read_analysis(ArtifactEvidence(current['artifact_ref'], current['artifact_sha256']))
                        merged = list(payload.get('package_sources') or [])
                        for item in candidate['package_sources']:
                            if item not in merged:
                                merged.append(item)
                        if merged != payload.get('package_sources', []):
                            # Preserve the existing analysis and human status; only add explicit provenance.
                            payload['package_sources'] = merged
                            analysis_batch = self.artifacts.stage_analysis(payload)
                            analysis_batches.append(analysis_batch)
                            evidence = analysis_batch.promote()
                            analysis_id = new_id('project_image_analysis')
                            connection.execute(
                                'INSERT INTO project_image_analysis_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                (analysis_id, image_id, workspace_id, project_id, brief_version_id,
                                 brief_snapshot_id, digest, current['status'], evidence.ref, evidence.sha256,
                                 current['created_by_actor_id'], current['created_at']),
                            )
                            connection.execute('UPDATE project_image_analysis_current_links SET project_image_analysis_version_id=?, updated_at=? WHERE project_image_id=?',
                                (analysis_id, created_at, image_id))
                    adopted.append(image_id)
                _confirmed_brief_context(
                    connection, workspace_id, project_id, brief_version_id,
                    brief_snapshot_id, expected_project_lock_version,
                )
        except Exception:
            for batch in reversed((*analysis_batches, *source_batches)):
                batch.rollback()
            raise
        for batch in (*source_batches, *analysis_batches):
            batch.commit()
        return tuple(dict.fromkeys(adopted))

    def adopt_local_upload(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        brief_version_id: str,
        brief_snapshot_id: str,
        expected_project_lock_version: int,
        filename: str,
        content: bytes,
    ) -> str:
        """Add one local image to the existing project authority without replacing its Brief."""
        safe_name = Path(str(filename or "")).name
        suffix = Path(safe_name).suffix.casefold()
        if not safe_name or suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise WorkspaceServiceError("INVALID_INPUT", "本地补图只接受 JPG、PNG 或 WebP")
        if not content or len(content) > 25 * 1024 * 1024:
            raise WorkspaceServiceError("FILE_TOO_LARGE", "本地补图为空或超过 25MB")
        try:
            with Image.open(BytesIO(content)) as image:
                image.verify()
        except (OSError, ValueError) as exc:
            raise WorkspaceServiceError("INVALID_INPUT", "本地补图不是可读取图片") from exc
        digest = sha256(content).hexdigest()
        temp_path = None
        source_batch = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as staged:
                staged.write(content)
                staged.flush()
                temp_path = Path(staged.name)
            source_batch = self.artifacts.stage_source_image(temp_path, digest)
            evidence = source_batch.promote()
            with self.store.transaction() as connection:
                _project_and_actor(
                    connection, auth_token, workspace_id, project_id, "project.update",
                )
                _confirmed_brief_context(
                    connection, workspace_id, project_id, brief_version_id,
                    brief_snapshot_id, expected_project_lock_version,
                )
                actor_id = _actor_id(connection, auth_token)
                created_at = utc_now()
                existing = connection.execute(
                    "SELECT project_image_id, artifact_ref FROM project_images WHERE project_id=? AND image_sha256=?",
                    (project_id, digest),
                ).fetchone()
                if existing is None:
                    image_id = new_id("project_image")
                    connection.execute(
                        "INSERT INTO project_images VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (image_id, workspace_id, project_id, digest, evidence.ref, actor_id, created_at),
                    )
                else:
                    image_id = str(existing["project_image_id"])
                    if existing["artifact_ref"] != evidence.ref:
                        raise WorkspaceServiceError("IMAGE_ARTIFACT_DRIFT", "项目同 SHA 图片引用漂移")
                media_id = f"local-{digest[:24]}"
                exists = connection.execute(
                    """SELECT 1 FROM project_image_package_source_links
                    WHERE project_image_id=? AND package_id='formal-local-upload'
                      AND package_version=1 AND media_id=?""",
                    (image_id, media_id),
                ).fetchone()
                if exists is None:
                    connection.execute(
                        """INSERT INTO project_image_package_source_links VALUES
                        (?, ?, 'formal-local-upload', 1, ?, ?, ?, '本地补图', 1, ?, ?)""",
                        (
                            new_id("project_image_package_source"), image_id, media_id,
                            digest, safe_name, actor_id, created_at,
                        ),
                    )
                _confirmed_brief_context(
                    connection, workspace_id, project_id, brief_version_id,
                    brief_snapshot_id, expected_project_lock_version,
                )
            source_batch.commit()
            return image_id
        except Exception:
            if source_batch is not None:
                source_batch.rollback()
            raise
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def adopt_generated_image(self, auth_token: str, *, workspace_id: str, project_id: str,
                              brief_version_id: str, brief_snapshot_id: str, expected_project_lock_version: int,
                              task_id: str, result_ref: str, result_sha256: str, metadata: dict) -> str:
        """Adopt a completed task's verified output without inventing source rows."""
        from .task_service import TaskService
        task = TaskService(self.store).get_task(auth_token, task_id)
        digest = sha256(_canonical_json(metadata).encode()).hexdigest()
        if (task['project_id'] != project_id or task['task_type'] != 'formal_image_creation'
                or task['status'] != 'completed' or task['result_ref'] != result_ref
                or result_ref != f'artifact://creative-image/{result_sha256}' or digest != result_sha256
                or metadata.get('task_id') != task_id or metadata.get('request_sha256') != task['payload_sha256']
                or metadata.get('origin') not in {'generated', 'edited'}):
            raise WorkspaceServiceError('IMAGE_ARTIFACT_DRIFT', '生成图片缺少一致的完成任务和来源证据')
        request = next((e['details']['image_request'] for e in TaskService(self.store).list_task_events(auth_token, task_id)
                        if e['details'].get('image_request')), None)
        if not request or metadata.get('parents') != request['references']:
            raise WorkspaceServiceError('IMAGE_ARTIFACT_DRIFT', '生成图片的原图关系与请求不一致')
        evidence = ArtifactEvidence(**metadata['image'])
        self.artifacts.read_source_image(evidence)
        with self.store.transaction() as connection:
            _, actor = _project_and_actor(connection, auth_token, workspace_id, project_id, 'project.update')
            _confirmed_brief_context(connection, workspace_id, project_id, brief_version_id, brief_snapshot_id, expected_project_lock_version)
            existing_source = connection.execute('SELECT * FROM project_image_generated_sources WHERE task_id=?', (task_id,)).fetchone()
            if existing_source:
                if existing_source['result_sha256'] != result_sha256:
                    raise WorkspaceServiceError('IMAGE_ARTIFACT_DRIFT', '该生成任务已采用其他结果')
                return str(existing_source['project_image_id'])
            existing = connection.execute('SELECT project_image_id, artifact_ref FROM project_images WHERE project_id=? AND image_sha256=?',
                (project_id, evidence.sha256)).fetchone()
            now = utc_now()
            image_id = str(existing['project_image_id']) if existing else new_id('project_image')
            if existing and existing['artifact_ref'] != evidence.ref:
                raise WorkspaceServiceError('IMAGE_ARTIFACT_DRIFT', '同一图片身份的来源引用不一致')
            if not existing:
                connection.execute('INSERT INTO project_images VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (image_id, workspace_id, project_id, evidence.sha256, evidence.ref, actor['actor_id'], now))
            connection.execute('INSERT INTO project_image_generated_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (task_id, image_id, metadata['origin'], result_ref, result_sha256,
                 _canonical_json(metadata['parents']), actor['actor_id'], now))
        return image_id

    def resolve_for_planning(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        brief_version_id: str, brief_snapshot_id: str, expected_project_lock_version: int,
        historical: bool = False,
    ) -> ProjectImagePlanningReadiness:
        with self.store.connection() as connection:
            _project_and_actor(connection, auth_token, workspace_id, project_id, "project.read")
            _confirmed_brief_context(connection, workspace_id, project_id, brief_version_id, brief_snapshot_id, expected_project_lock_version, require_current=not historical)
            images = self._read_images(connection, workspace_id, project_id, brief_version_id, brief_snapshot_id)
            # Artifact validation lies between two current/lock checks, so a concurrent switch fails closed.
            for image in images:
                source_content = self.artifacts.read_source_image(ArtifactEvidence(image["artifact_ref"], image["image_sha256"]))
                analysis = image.get("analysis")
                if analysis is not None:
                    value = self.artifacts.read_analysis(analysis.evidence)
                    image["summary"] = _validate_analysis_payload(
                        value, image, workspace_id, project_id,
                        brief_version_id, brief_snapshot_id, analysis, source_content,
                    )
                    links = list(image["links"])
                    for origin in value.get("package_sources", []):
                        for source in origin.get("sources", []):
                            location = ProjectImageSourceLocation(source_label=str(source["file"]),
                                sheet=str(source["sheet"]), row=int(source["row"]),
                                source_document_id=f"package:{origin['package_id']}@{origin['package_version']}:{origin['media_id']}")
                            if location not in links:
                                links.append(location)
                    image["links"] = tuple(links)
            _confirmed_brief_context(connection, workspace_id, project_id, brief_version_id, brief_snapshot_id, expected_project_lock_version, require_current=not historical)
        projected = tuple(_project_image(item) for item in images)
        statuses = [item.current_analysis.status if item.current_analysis else "pending" for item in projected]
        return ProjectImagePlanningReadiness(
            workspace_id, project_id, brief_version_id, brief_snapshot_id,
            len(projected), statuses.count("pass"), statuses.count("pending"),
            statuses.count("low_confidence"), statuses.count("blocked"), statuses.count("failed"), projected,
        )

    def employee_readiness(self, readiness: ProjectImagePlanningReadiness) -> dict:
        if not isinstance(readiness, ProjectImagePlanningReadiness):
            raise TypeError("员工图片 DTO 只接受 ProjectImagePlanningReadiness")
        attention_count = (
            readiness.pending + readiness.low_confidence
            + readiness.blocked + readiness.failed
        )
        return {
            "total": readiness.total, "pass": readiness.passed, "pending": readiness.pending,
            "low_confidence": readiness.low_confidence, "blocked": readiness.blocked, "failed": readiness.failed,
            "usable_count": readiness.passed,
            "attention_count": attention_count,
            "can_plan_with_usable_images": readiness.passed > 0,
            "can_continue_text_only": True,
            "analysis_options": {
                "quality_flags": sorted(PROJECT_IMAGE_QUALITY_FLAGS),
                "negative_flags": sorted(PROJECT_IMAGE_NEGATIVE_FLAGS),
            },
            "images": [{
                "image_key": image.image_id, "status": image.current_analysis.status if image.current_analysis else "pending",
                "analysis_key": image.current_analysis.analysis_version_id if image.current_analysis else None,
                "summary": None if image.business_summary is None else {
                    "caption": image.business_summary.caption,
                    "tags": list(image.business_summary.tags),
                    "subjects": list(image.business_summary.subjects),
                    "scene": image.business_summary.scene,
                    "purpose": image.business_summary.purpose,
                    "quality": image.business_summary.quality,
                    "brand": image.business_summary.brand,
                    "evidence_status": image.business_summary.evidence_status,
                    "quality_flags": list(image.business_summary.quality_flags),
                    "negative_flags": list(image.business_summary.negative_flags),
                    "pixel_width": image.business_summary.pixel_width,
                    "pixel_height": image.business_summary.pixel_height,
                },
                "sources": [_employee_source(source) for source in image.source_links],
            } for image in readiness.images],
        }

    def read_preview_image(
        self, auth_token: str, *, workspace_id: str, project_id: str, image_id: str,
        brief_version_id: str, brief_snapshot_id: str, expected_project_lock_version: int,
        variant: str = "original", historical: bool = False,
    ) -> tuple[bytes, str]:
        """Return verified bytes for a current project image; never expose storage identity."""
        with self.store.connection() as connection:
            _project_and_actor(connection, auth_token, workspace_id, project_id, "project.read")
            _confirmed_brief_context(connection, workspace_id, project_id, brief_version_id, brief_snapshot_id, expected_project_lock_version, require_current=not historical)
            row = connection.execute(
                "SELECT image_sha256, artifact_ref FROM project_images WHERE project_image_id=? AND workspace_id=? AND project_id=?",
                (image_id, workspace_id, project_id),
            ).fetchone()
            if row is None:
                raise WorkspaceServiceError("NOT_FOUND", "当前项目不存在目标图片")
            content = self.artifacts.read_source_image(ArtifactEvidence(str(row["artifact_ref"]), str(row["image_sha256"])))
            preview = ((content, _safe_image_mime(content)) if variant == "original" else
                image_preview_bytes(content, content_sha256=str(row["image_sha256"]),
                    variant=variant, cache_root=self.artifacts.root.parent / "derived-image-previews"))
            _confirmed_brief_context(connection, workspace_id, project_id, brief_version_id, brief_snapshot_id, expected_project_lock_version, require_current=not historical)
        return preview

    def reuse_analysis_for_current_brief(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        image_id: str, image_sha256: str, brief_version_id: str,
        brief_snapshot_id: str, expected_project_lock_version: int,
    ) -> ProjectImageAnalysis | None:
        """Reuse a verified same-image observation; never inherit an old approval."""
        with self.store.atomic() as connection:
            _project_and_actor(connection, auth_token, workspace_id, project_id, "review.record")
            _confirmed_brief_context(connection, workspace_id, project_id,
                brief_version_id, brief_snapshot_id, expected_project_lock_version)
            image = next((item for item in self._read_images(connection, workspace_id,
                project_id, brief_version_id, brief_snapshot_id) if item['image_id'] == image_id), None)
            if image is None or image['image_sha256'] != image_sha256:
                raise WorkspaceServiceError('IMAGE_ARTIFACT_DRIFT', '复用观察的图片身份已变化')
            if image['analysis'] is not None:
                return image['analysis']
            row = connection.execute('''SELECT a.* FROM project_image_analysis_current_links l
                JOIN project_image_analysis_versions a ON a.project_image_analysis_version_id=l.project_image_analysis_version_id
                WHERE l.project_image_id=?''', (image_id,)).fetchone()
            if row is None or row['image_sha256'] != image_sha256:
                return None
            prior = ProjectImageAnalysis(str(row['project_image_analysis_version_id']), str(row['status']),
                ArtifactEvidence(str(row['artifact_ref']), str(row['artifact_sha256'])),
                str(row['created_by_actor_id']), str(row['created_at']))
            value = self.artifacts.read_analysis(prior.evidence)
            content = self.artifacts.read_source_image(ArtifactEvidence(image['artifact_ref'], image_sha256))
            _validate_analysis_payload(value, image, workspace_id, project_id,
                str(row['brief_version_id']), str(row['brief_snapshot_id']), prior, content)
            if prior.status == 'failed' or value.get('visual_analysis_available') is False:
                return None
            source = {'kind': 'analysis', 'analysis_key': prior.analysis_version_id,
                'artifact_ref': prior.evidence.ref, 'artifact_sha256': prior.evidence.sha256,
                'brief_version_id': str(row['brief_version_id']),
                'brief_snapshot_id': str(row['brief_snapshot_id'])}
            return self._record_reused_observation(auth_token, workspace_id=workspace_id,
                project_id=project_id, image_id=image_id, image_sha256=image_sha256,
                brief_version_id=brief_version_id, brief_snapshot_id=brief_snapshot_id,
                expected_project_lock_version=expected_project_lock_version, value=value, source=source)

    def _record_reused_observation(self, auth_token: str, *, workspace_id: str, project_id: str,
            image_id: str, image_sha256: str, brief_version_id: str, brief_snapshot_id: str,
            expected_project_lock_version: int, value: dict, source: dict) -> ProjectImageAnalysis:
        batch = None
        try:
            with self.store.transaction() as connection:
                _project_and_actor(connection, auth_token, workspace_id, project_id, 'review.record')
                _confirmed_brief_context(connection, workspace_id, project_id,
                    brief_version_id, brief_snapshot_id, expected_project_lock_version)
                image = connection.execute('SELECT * FROM project_images WHERE project_image_id=? AND workspace_id=? AND project_id=?',
                    (image_id, workspace_id, project_id)).fetchone()
                if image is None or image['image_sha256'] != image_sha256:
                    raise WorkspaceServiceError('IMAGE_ARTIFACT_DRIFT', '原观察与当前图片身份不一致')
                current = connection.execute('''SELECT a.* FROM project_image_analysis_current_links l
                    JOIN project_image_analysis_versions a ON a.project_image_analysis_version_id=l.project_image_analysis_version_id
                    WHERE l.project_image_id=?''', (image_id,)).fetchone()
                if current and current['brief_version_id'] == brief_version_id and current['brief_snapshot_id'] == brief_snapshot_id:
                    return ProjectImageAnalysis(str(current['project_image_analysis_version_id']), str(current['status']),
                        ArtifactEvidence(str(current['artifact_ref']), str(current['artifact_sha256'])),
                        str(current['created_by_actor_id']), str(current['created_at']))
                content = self.artifacts.read_source_image(ArtifactEvidence(str(image['artifact_ref']), image_sha256))
                actor_id, created_at = _actor_id(connection, auth_token), utc_now()
                status = 'blocked' if value['status'] == 'blocked' else 'low_confidence'
                payload = {key: value[key] for key in ('caption', 'tags', 'subjects', 'scene', 'quality', 'brand', 'evidence')}
                original_purpose = value.get('original_purpose', value['purpose'])
                payload.update(schema_version='mplus-project-image-analysis/v5', analysis_origin='reused_observation',
                    workspace_id=workspace_id, project_id=project_id, project_image_id=image_id,
                    image_sha256=image_sha256, brief_version_id=brief_version_id, brief_snapshot_id=brief_snapshot_id,
                    status=status, purpose='用途待按当前需求核对；原分析参考：' + original_purpose,
                    original_purpose=original_purpose, quality_flags=list(value.get('quality_flags', [])),
                    negative_flags=list(value.get('negative_flags', [])), visual_observations=_visual_observations(value.get('visual_observations', {})),
                    rebound_by_actor_id=actor_id, rebound_at=created_at, observation_source=source,
                    **project_image_pixel_evidence(content))
                batch = self.artifacts.stage_analysis(payload)
                analysis_id = new_id('project_image_analysis')
                connection.execute('INSERT INTO project_image_analysis_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (analysis_id, image_id, workspace_id, project_id, brief_version_id, brief_snapshot_id,
                     image_sha256, status, batch.evidence.ref, batch.evidence.sha256, actor_id, created_at))
                connection.execute('''INSERT INTO project_image_analysis_current_links VALUES (?, ?, ?)
                    ON CONFLICT(project_image_id) DO UPDATE SET project_image_analysis_version_id=excluded.project_image_analysis_version_id, updated_at=excluded.updated_at''',
                    (image_id, analysis_id, created_at))
                batch.promote()
        except Exception:
            if batch is not None:
                batch.rollback()
            raise
        batch.commit()
        return ProjectImageAnalysis(analysis_id, status, batch.evidence, actor_id, created_at)

    def resolve_pending_for_provider(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        brief_version_id: str, brief_snapshot_id: str,
        expected_project_lock_version: int,
    ) -> ProjectImageProviderPool:
        """Return pending images as verified server paths for the vision route."""
        readiness = self.resolve_for_planning(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            brief_version_id=brief_version_id,
            brief_snapshot_id=brief_snapshot_id,
            expected_project_lock_version=expected_project_lock_version,
        )
        assets = []
        for image in readiness.images:
            if image.current_analysis is not None:
                continue
            evidence = ArtifactEvidence(
                f"artifact://formal-image/{image.image_sha256}", image.image_sha256,
            )
            self.artifacts.read_source_image(evidence)
            assets.append(ProjectImageProviderAsset(
                image.image_id,
                image.image_sha256,
                self.artifacts.root / "formal-images" / image.image_sha256[:2] / image.image_sha256,
                image.source_links,
            ))
        return ProjectImageProviderPool(
            readiness.workspace_id,
            readiness.project_id,
            readiness.brief_version_id,
            readiness.brief_snapshot_id,
            tuple(assets),
        )

    def resolve_for_delivery(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        brief_version_id: str, brief_snapshot_id: str,
        expected_project_lock_version: int,
        historical: bool = False,
    ) -> ProjectImageDeliveryPool:
        """Return current pass-only assets with verified server-controlled paths."""
        readiness = self.resolve_for_planning(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            brief_version_id=brief_version_id,
            brief_snapshot_id=brief_snapshot_id,
            expected_project_lock_version=expected_project_lock_version,
            historical=historical,
        )
        assets = []
        for image in readiness.images:
            if image.current_analysis is None or image.current_analysis.status != "pass":
                continue
            if image.business_summary is None:
                raise WorkspaceServiceError("IMAGE_ANALYSIS_REQUIRED", "项目图片缺少已通过的理解结果")
            evidence = ArtifactEvidence(
                f"artifact://formal-image/{image.image_sha256}", image.image_sha256,
            )
            self.artifacts.read_source_image(evidence)
            path = self.artifacts.root / "formal-images" / image.image_sha256[:2] / image.image_sha256
            assets.append(ProjectImageDeliveryAsset(
                image.image_id,
                image.image_sha256,
                path,
                image.source_links,
                image.current_analysis,
                image.business_summary,
            ))
        return ProjectImageDeliveryPool(
            readiness.workspace_id,
            readiness.project_id,
            readiness.brief_version_id,
            readiness.brief_snapshot_id,
            tuple(assets),
            readiness.pending,
            readiness.low_confidence,
            readiness.blocked,
            readiness.failed,
        )

    @staticmethod
    def _read_images(connection, workspace_id: str, project_id: str, brief_version_id: str, brief_snapshot_id: str) -> list[dict]:
        rows = connection.execute(
            """SELECT i.project_image_id, i.image_sha256, i.artifact_ref,
                      a.project_image_analysis_version_id, a.status, a.artifact_ref AS analysis_ref,
                      a.artifact_sha256, a.created_by_actor_id, a.created_at,
                      a.brief_version_id AS analysis_brief_version_id,
                      a.brief_snapshot_id AS analysis_brief_snapshot_id
               FROM project_images i
               LEFT JOIN project_image_analysis_current_links l ON l.project_image_id=i.project_image_id
               LEFT JOIN project_image_analysis_versions a ON a.project_image_analysis_version_id=l.project_image_analysis_version_id
               WHERE i.workspace_id=? AND i.project_id=? ORDER BY i.project_image_id""",
            (workspace_id, project_id),
        ).fetchall()
        result = []
        for row in rows:
            analysis = None
            if (
                row["project_image_analysis_version_id"] is not None
                and row["analysis_brief_version_id"] == brief_version_id
                and row["analysis_brief_snapshot_id"] == brief_snapshot_id
            ):
                analysis = ProjectImageAnalysis(
                    str(row["project_image_analysis_version_id"]), str(row["status"]),
                    ArtifactEvidence(str(row["analysis_ref"]), str(row["artifact_sha256"])),
                    str(row["created_by_actor_id"]), str(row["created_at"]),
                )
            links = connection.execute(
                """SELECT l.source_document_id, l.source_location_json, f.filename
                   FROM project_image_source_links l
                   JOIN document_images d ON d.document_image_id=l.document_image_id
                   JOIN project_files f ON f.file_id=d.file_id
                   WHERE l.project_image_id=? ORDER BY l.project_image_source_link_id""",
                (row["project_image_id"],),
            ).fetchall()
            package_links = connection.execute(
                """SELECT package_id, package_version, media_id, source_file,
                          source_sheet, source_row
                   FROM project_image_package_source_links
                   WHERE project_image_id=?
                   ORDER BY package_id, package_version, media_id""",
                (row["project_image_id"],),
            ).fetchall()
            projected_links = [
                _source_location(
                    str(link["filename"]),
                    _json_object(link["source_location_json"], "source_location_json"),
                    str(link["source_document_id"]),
                )
                for link in links
            ]
            projected_links.extend(
                ProjectImageSourceLocation(
                    source_label=str(link["source_file"]),
                    sheet=str(link["source_sheet"]), row=int(link["source_row"]),
                    source_document_id=(
                        f"package:{link['package_id']}@{link['package_version']}:{link['media_id']}"
                    ),
                )
                for link in package_links
            )
            generated = connection.execute('SELECT task_id, origin FROM project_image_generated_sources WHERE project_image_id=? ORDER BY task_id',
                (row['project_image_id'],)).fetchall()
            projected_links.extend(ProjectImageSourceLocation(
                source_label='AI 编辑图' if link['origin'] == 'edited' else 'AI 生成图',
                source_document_id='generated:' + str(link['task_id'])) for link in generated)
            result.append({
                "image_id": str(row["project_image_id"]), "image_sha256": str(row["image_sha256"]),
                "workspace_id": workspace_id, "project_id": project_id,
                "artifact_ref": str(row["artifact_ref"]),
                "links": tuple(projected_links),
                "analysis": analysis, "summary": None,
            })
        return result


class ManualProjectImageAnalysisCommandService:
    """Manual command boundary. It is intentionally not an Agent action."""

    def __init__(self, pool: ProjectImagePoolService):
        self.pool = pool

    def _confirmation_result(self, connection, actor_id: str, project_id: str, key: str):
        row = connection.execute("""SELECT i.request_sha256, r.response_json
            FROM manual_command_idempotency i JOIN manual_command_results r ON r.command_id=i.command_id
            WHERE i.actor_id=? AND i.project_id=? AND i.tool_name='confirm_existing_analyses'
              AND i.idempotency_key=? AND i.status='consumed'""", (actor_id, project_id, key)).fetchone()
        return (row["request_sha256"], json.loads(row["response_json"])) if row else None

    def read_confirmation(self, auth_token: str, *, workspace_id: str, project_id: str,
                          idempotency_key: str) -> dict:
        with self.pool.store.connection() as connection:
            _, actor = _project_and_actor(connection, auth_token, workspace_id, project_id, "review.record")
            result = self._confirmation_result(connection, str(actor["actor_id"]), project_id, idempotency_key)
        if result is None:
            return {"confirmation_status": "not_found"}
        return {name: value for name, value in result[1].items() if name != "analyses"}

    @staticmethod
    def _confirmed_results(result: dict) -> tuple[ProjectImageAnalysis, ...]:
        return tuple(ProjectImageAnalysis(item["analysis_version_id"], item["status"],
            ArtifactEvidence(item["artifact_ref"], item["artifact_sha256"]),
            item["created_by_actor_id"], item["created_at"]) for item in result["analyses"])

    def confirm_existing_analyses(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        expected_project_lock_version: int, selections: list[dict], note: str = "",
        idempotency_key: str,
    ) -> tuple[ProjectImageAnalysis, ...]:
        """Confirm only the displayed analyses, validating the entire batch first."""
        if (not isinstance(selections, list) or not selections or len(selections) > 400
                or any(not isinstance(item, dict) or set(item) != {"image_key", "analysis_key"}
                       or not all(isinstance(value, str) and value for value in item.values()) for item in selections)
                or len({item["image_key"] for item in selections}) != len(selections)):
            raise WorkspaceServiceError("IMAGE_ANALYSIS_SELECTION_INVALID", "请选择具有可查看分析结果的图片")
        if not isinstance(note, str) or len(note) > 1000 or not idempotency_key or len(idempotency_key) > 160:
            raise WorkspaceServiceError("INVALID_INPUT", "共同意见或操作标识无效")
        request_sha = sha256(json.dumps({"project_id": project_id, "project_version": expected_project_lock_version,
            "selections": selections, "note": note, "idempotency_key": idempotency_key},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        # Recover an already committed result before checking a newer project/analysis.
        with self.pool.store.connection() as connection:
            _, actor = _project_and_actor(connection, auth_token, workspace_id, project_id, "review.record")
            replay = self._confirmation_result(connection, str(actor["actor_id"]), project_id, idempotency_key)
            if replay:
                if replay[0] != request_sha:
                    raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "同一操作标识已用于另一批确认，请重新核对")
                return self._confirmed_results(replay[1])
        brief_id, snapshot_id = self.pool.current_confirmed_brief_binding(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_lock_version=expected_project_lock_version,
        )
        batches, results = [], []
        try:
            with self.pool.store.transaction() as connection:
                _, actor = _project_and_actor(connection, auth_token, workspace_id, project_id, "review.record")
                replay = self._confirmation_result(connection, str(actor["actor_id"]), project_id, idempotency_key)
                if replay:
                    if replay[0] != request_sha:
                        raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "同一操作标识已用于另一批确认，请重新核对")
                    return self._confirmed_results(replay[1])
                _confirmed_brief_context(connection, workspace_id, project_id, brief_id, snapshot_id, expected_project_lock_version)
                from .manual_version_commands import record_atomic_manual_confirmation
                record_atomic_manual_confirmation(connection,
                    context={"actor_id": str(actor["actor_id"]), "project_id": project_id},
                    action="confirm_existing_analyses", idempotency_key=idempotency_key, digest=request_sha)
                images = {item["image_id"]: item for item in self.pool._read_images(connection, workspace_id, project_id, brief_id, snapshot_id)}
                ready = []
                for selection in selections:
                    image = images.get(selection["image_key"])
                    if image is None or image.get("analysis") is None:
                        raise WorkspaceServiceError("IMAGE_ANALYSIS_SELECTION_INVALID", "所选图片没有可核对分析，未写入任何确认")
                    analysis = image["analysis"]
                    value = self.pool.artifacts.read_analysis(analysis.evidence)
                    source_content = self.pool.artifacts.read_source_image(ArtifactEvidence(image["artifact_ref"], image["image_sha256"]))
                    summary = _validate_analysis_payload(value, image, workspace_id, project_id, brief_id, snapshot_id, analysis, source_content)
                    confirmation = value.get("human_confirmation") or {}
                    replay = (confirmation.get("analysis_key") == selection["analysis_key"]
                              and confirmation.get("request_sha256") == request_sha)
                    if analysis.analysis_version_id != selection["analysis_key"] and not replay:
                        raise WorkspaceServiceError("IMAGE_ANALYSIS_VERSION_CONFLICT", "所选分析结果已变化，请重新核对；本批次未写入")
                    if (analysis.status in {"failed", "blocked"} or value.get("visual_analysis_available") is False
                            or set(summary.negative_flags).intersection({"forbidden_content", "sensitive_content"})):
                        raise WorkspaceServiceError("IMAGE_ANALYSIS_NOT_CONFIRMABLE", "所选分析缺少可核对结果或含明确禁用内容，请逐项处理")
                    ready.append((image, analysis, value, replay or (summary.evidence_status == "verified" and analysis.status == "pass")))
                actor_id, created_at = str(actor["actor_id"]), utc_now()
                for image, analysis, value, unchanged in ready:
                    if unchanged:
                        results.append(analysis)
                        continue
                    payload = {**value, "status": "pass", "reviewed_by_actor_id": actor_id, "reviewed_at": created_at,
                        "human_confirmation": {"analysis_key": analysis.analysis_version_id,
                            "evidence_ref": analysis.evidence.ref, "evidence_sha256": analysis.evidence.sha256,
                            "created_by_actor_id": analysis.created_by_actor_id, "created_at": analysis.created_at,
                            "status": analysis.status, "note": note, "idempotency_key": idempotency_key,
                            "request_sha256": request_sha}}
                    batch = self.pool.artifacts.stage_analysis(payload)
                    batches.append(batch)
                    analysis_id = new_id("project_image_analysis")
                    connection.execute("INSERT INTO project_image_analysis_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (analysis_id, image["image_id"], workspace_id, project_id, brief_id, snapshot_id,
                         image["image_sha256"], "pass", batch.evidence.ref, batch.evidence.sha256, actor_id, created_at))
                    connection.execute("UPDATE project_image_analysis_current_links SET project_image_analysis_version_id=?, updated_at=? WHERE project_image_id=?",
                        (analysis_id, created_at, image["image_id"]))
                    results.append(ProjectImageAnalysis(analysis_id, "pass", batch.evidence, actor_id, created_at))
                for batch in batches:
                    batch.promote()
                response = {
                    "confirmation_status": "completed", "confirmed_count": len(results),
                    "newly_confirmed_count": len(batches), "already_confirmed_count": len(results) - len(batches),
                    "analyses": [{"analysis_version_id": item.analysis_version_id, "status": item.status,
                        "artifact_ref": item.evidence.ref, "artifact_sha256": item.evidence.sha256,
                        "created_by_actor_id": item.created_by_actor_id, "created_at": item.created_at}
                        for item in results],
                }
                command = connection.execute("""SELECT command_id FROM manual_command_idempotency
                    WHERE actor_id=? AND project_id=? AND tool_name='confirm_existing_analyses' AND idempotency_key=?""",
                    (actor_id, project_id, idempotency_key)).fetchone()
                connection.execute("INSERT INTO manual_command_results VALUES (?, ?)",
                    (command["command_id"], json.dumps(response, ensure_ascii=False, sort_keys=True)))
        except Exception:
            for batch in batches:
                batch.rollback()
            raise
        for batch in batches:
            batch.commit()
        return tuple(results)

    def record_analysis(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        submission: ManualImageAnalysisInput, expected_project_lock_version: int,
        expected_analysis_key: str | None = None,
    ) -> ProjectImageAnalysis:
        if not isinstance(submission, ManualImageAnalysisInput):
            raise TypeError("人工图片分析只接受 ManualImageAnalysisInput")
        batch = None
        try:
            with self.pool.store.transaction() as connection:
                _project_and_actor(connection, auth_token, workspace_id, project_id, "review.record")
                image = connection.execute(
                "SELECT * FROM project_images WHERE project_image_id=? AND workspace_id=? AND project_id=?",
                (submission.image_id, workspace_id, project_id),
                ).fetchone()
                if image is None:
                    _other = connection.execute("SELECT 1 FROM project_images WHERE project_image_id=?", (submission.image_id,)).fetchone()
                    raise WorkspaceServiceError("CROSS_PROJECT_DENIED" if _other else "NOT_FOUND", "项目图片不存在或不属于项目")
                if str(image["image_sha256"]) != submission.image_sha256:
                    raise WorkspaceServiceError("IMAGE_ARTIFACT_DRIFT", "人工分析图片 SHA-256 不匹配")
                _confirmed_brief_context(connection, workspace_id, project_id, submission.brief_version_id, submission.brief_snapshot_id, expected_project_lock_version)
                source_content = self.pool.artifacts.read_source_image(ArtifactEvidence(str(image["artifact_ref"]), submission.image_sha256))
                existing = connection.execute(
                """SELECT a.* FROM project_image_analysis_current_links l
                    JOIN project_image_analysis_versions a ON a.project_image_analysis_version_id=l.project_image_analysis_version_id
                    WHERE l.project_image_id=?""", (submission.image_id,),
                ).fetchone()
                if expected_analysis_key is not None and str(existing["project_image_analysis_version_id"] if existing else "") != expected_analysis_key:
                    raise WorkspaceServiceError("IMAGE_ANALYSIS_VERSION_CONFLICT", "分析结果已变化，原输入保留，请核对最新分析后重新保存")
                if existing is not None and existing["status"] == "pass" and existing["brief_version_id"] == submission.brief_version_id and existing["brief_snapshot_id"] == submission.brief_snapshot_id:
                    existing_evidence = ArtifactEvidence(
                        str(existing["artifact_ref"]), str(existing["artifact_sha256"]),
                    )
                    existing_payload = self.pool.artifacts.read_analysis(existing_evidence)
                    submitted_fields = {
                        name: list(value) if isinstance(value, tuple) else value
                        for name in ("status", "caption", "tags", "subjects", "scene", "purpose",
                                     "quality", "brand", "evidence", "quality_flags", "negative_flags")
                        for value in (getattr(submission, name),)
                    }
                    if (existing_payload.get("analysis_origin") == "manual"
                            and all(existing_payload.get(name, [] if isinstance(value, list) else "") == value
                                    for name, value in submitted_fields.items())):
                        return ProjectImageAnalysis(str(existing["project_image_analysis_version_id"]), "pass", existing_evidence, str(existing["created_by_actor_id"]), str(existing["created_at"]))
                actor_id = _actor_id(connection, auth_token)
                created_at = utc_now()
                metrics = project_image_pixel_evidence(source_content)
                payload = {
                "schema_version": "mplus-project-image-analysis/v3", "analysis_origin": "manual", "workspace_id": workspace_id,
                "project_id": project_id, "project_image_id": submission.image_id,
                "image_sha256": submission.image_sha256, "brief_version_id": submission.brief_version_id,
                "brief_snapshot_id": submission.brief_snapshot_id, "status": submission.status,
                "caption": submission.caption, "tags": list(submission.tags), "subjects": list(submission.subjects),
                "scene": submission.scene, "purpose": submission.purpose, "quality": submission.quality,
                "brand": submission.brand, "evidence": list(submission.evidence),
                "quality_flags": list(submission.quality_flags), "negative_flags": list(submission.negative_flags),
                **metrics,
                "reviewed_by_actor_id": actor_id, "reviewed_at": created_at,
                }
                batch = self.pool.artifacts.stage_analysis(payload)
                evidence = batch.evidence
                analysis_id = new_id("project_image_analysis")
                connection.execute(
                """INSERT INTO project_image_analysis_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (analysis_id, submission.image_id, workspace_id, project_id, submission.brief_version_id,
                 submission.brief_snapshot_id, submission.image_sha256, submission.status, evidence.ref,
                 evidence.sha256, actor_id, created_at),
                )
                current = connection.execute("SELECT 1 FROM project_image_analysis_current_links WHERE project_image_id=?", (submission.image_id,)).fetchone()
                if current is None:
                    connection.execute("INSERT INTO project_image_analysis_current_links VALUES (?, ?, ?)", (submission.image_id, analysis_id, created_at))
                else:
                    connection.execute("UPDATE project_image_analysis_current_links SET project_image_analysis_version_id=?, updated_at=? WHERE project_image_id=?", (analysis_id, created_at, submission.image_id))
                _confirmed_brief_context(connection, workspace_id, project_id, submission.brief_version_id, submission.brief_snapshot_id, expected_project_lock_version)
                batch.promote()
        except Exception:
            if batch is not None:
                batch.rollback()
            raise
        assert batch is not None
        batch.commit()
        return ProjectImageAnalysis(analysis_id, submission.status, evidence, actor_id, created_at)


class ProviderProjectImageAnalysisCommandService:
    """Server-only provider write boundary. It never claims human review."""

    def __init__(self, pool: ProjectImagePoolService):
        self.pool = pool

    def record_returned_analysis(self, auth_token: str, *, workspace_id: str, project_id: str,
            submission: ProviderImageAnalysisInput, expected_project_lock_version: int,
            observation_brief_version_id: str, observation_brief_snapshot_id: str,
            source_task_id: str) -> ProjectImageAnalysis:
        """Commit only the exact durable return of this actor's image-analysis task."""
        if not isinstance(submission, ProviderImageAnalysisInput):
            raise TypeError('自动图片分析只接受 ProviderImageAnalysisInput')
        expected = asdict(submission)
        result = {key: expected[key] for key in ('status', 'caption', 'tags', 'subjects',
            'scene', 'purpose', 'quality', 'brand', 'evidence', 'quality_flags', 'negative_flags', 'visual_observations')}
        receipt = {'result': result, 'provider': submission.provider, 'model': submission.model,
            'prompt_version': submission.prompt_version, 'brief_version_id': observation_brief_version_id,
            'brief_snapshot_id': observation_brief_snapshot_id}
        normalized = json.loads(json.dumps(receipt, ensure_ascii=False))
        with self.pool.store.atomic() as connection:
            _project_and_actor(connection, auth_token, workspace_id, project_id, 'review.record')
            task = connection.execute('SELECT * FROM tasks WHERE task_id=?', (source_task_id,)).fetchone()
            if (task is None or task['project_id'] != project_id
                    or task['actor_id'] != _actor_id(connection, auth_token)
                    or task['task_type'] != 'formal_image_analysis'):
                raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '原分析回执不属于当前任务身份')
            rows = connection.execute('SELECT details_json FROM task_events WHERE task_id=? ORDER BY sequence_no DESC',
                (source_task_id,)).fetchall()
            found = False
            for row in rows:
                data = json.loads(row['details_json']).get('image_analysis_batch', {})
                if data.get('workspace_id') != workspace_id or data.get('project_id') != project_id:
                    continue
                if any(item.get('image_key') == submission.image_id and item.get('image_sha256') == submission.image_sha256
                       and item.get('receipt') == normalized for item in data.get('items', [])):
                    found = True
                    break
            if not found:
                raise WorkspaceServiceError('IMAGE_ANALYSIS_RECEIPT_MISMATCH', '原分析返回尚未落盘或与本次写入不一致')
            if (submission.brief_version_id, submission.brief_snapshot_id) == (
                    observation_brief_version_id, observation_brief_snapshot_id):
                return self.record_analysis(auth_token, workspace_id=workspace_id, project_id=project_id,
                    submission=submission, expected_project_lock_version=expected_project_lock_version)
            from .run_contract import sha256_json
            return self.pool._record_reused_observation(auth_token, workspace_id=workspace_id,
                project_id=project_id, image_id=submission.image_id, image_sha256=submission.image_sha256,
                brief_version_id=submission.brief_version_id, brief_snapshot_id=submission.brief_snapshot_id,
                expected_project_lock_version=expected_project_lock_version, value=json.loads(json.dumps(result)),
                source={'kind': 'task_return', 'task_id': source_task_id,
                    'receipt_sha256': sha256_json(normalized), 'provider': submission.provider,
                    'model': submission.model, 'prompt_version': submission.prompt_version,
                    'brief_version_id': observation_brief_version_id,
                    'brief_snapshot_id': observation_brief_snapshot_id})

    def record_analysis(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        submission: ProviderImageAnalysisInput, expected_project_lock_version: int,
    ) -> ProjectImageAnalysis:
        if not isinstance(submission, ProviderImageAnalysisInput):
            raise TypeError("自动图片分析只接受 ProviderImageAnalysisInput")
        batch = None
        try:
            with self.pool.store.transaction() as connection:
                _project_and_actor(
                    connection, auth_token, workspace_id, project_id, "review.record",
                )
                image = connection.execute(
                    "SELECT * FROM project_images WHERE project_image_id=? AND workspace_id=? AND project_id=?",
                    (submission.image_id, workspace_id, project_id),
                ).fetchone()
                if image is None:
                    other = connection.execute(
                        "SELECT 1 FROM project_images WHERE project_image_id=?",
                        (submission.image_id,),
                    ).fetchone()
                    raise WorkspaceServiceError(
                        "CROSS_PROJECT_DENIED" if other else "NOT_FOUND",
                        "项目图片不存在或不属于项目",
                    )
                if str(image["image_sha256"]) != submission.image_sha256:
                    raise WorkspaceServiceError(
                        "IMAGE_ARTIFACT_DRIFT", "自动分析图片 SHA-256 不匹配",
                    )
                _confirmed_brief_context(
                    connection, workspace_id, project_id,
                    submission.brief_version_id, submission.brief_snapshot_id,
                    expected_project_lock_version,
                )
                source_content = self.pool.artifacts.read_source_image(ArtifactEvidence(
                    str(image["artifact_ref"]), submission.image_sha256,
                ))
                existing = connection.execute(
                    """SELECT a.* FROM project_image_analysis_current_links l
                        JOIN project_image_analysis_versions a
                          ON a.project_image_analysis_version_id=l.project_image_analysis_version_id
                        WHERE l.project_image_id=?""",
                    (submission.image_id,),
                ).fetchone()
                if existing is not None and (
                    existing["brief_version_id"] == submission.brief_version_id
                    and existing["brief_snapshot_id"] == submission.brief_snapshot_id
                ):
                    return ProjectImageAnalysis(
                        str(existing["project_image_analysis_version_id"]),
                        str(existing["status"]),
                        ArtifactEvidence(
                            str(existing["artifact_ref"]), str(existing["artifact_sha256"]),
                        ),
                        str(existing["created_by_actor_id"]), str(existing["created_at"]),
                    )
                actor_id = _actor_id(connection, auth_token)
                created_at = utc_now()
                metrics = project_image_pixel_evidence(source_content)
                payload = {
                    "schema_version": "mplus-project-image-analysis/v3",
                    "analysis_origin": "provider",
                    "workspace_id": workspace_id,
                    "project_id": project_id,
                    "project_image_id": submission.image_id,
                    "image_sha256": submission.image_sha256,
                    "brief_version_id": submission.brief_version_id,
                    "brief_snapshot_id": submission.brief_snapshot_id,
                    "status": submission.status,
                    "caption": submission.caption,
                    "tags": list(submission.tags),
                    "subjects": list(submission.subjects),
                    "scene": submission.scene,
                    "purpose": submission.purpose,
                    "quality": submission.quality,
                    "brand": submission.brand,
                    "evidence": list(submission.evidence),
                    "quality_flags": list(submission.quality_flags),
                    "negative_flags": list(submission.negative_flags),
                    **metrics,
                    "provider": submission.provider,
                    "model": submission.model,
                    "prompt_version": submission.prompt_version,
                    "requested_by_actor_id": actor_id,
                    "generated_at": created_at,
                }
                if submission.visual_observations:
                    payload["schema_version"] = "mplus-project-image-analysis/v4"
                    payload["visual_observations"] = _visual_observations(submission.visual_observations)
                batch = self.pool.artifacts.stage_analysis(payload)
                evidence = batch.evidence
                analysis_id = new_id("project_image_analysis")
                connection.execute(
                    """INSERT INTO project_image_analysis_versions
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        analysis_id, submission.image_id, workspace_id, project_id,
                        submission.brief_version_id, submission.brief_snapshot_id,
                        submission.image_sha256, submission.status, evidence.ref,
                        evidence.sha256, actor_id, created_at,
                    ),
                )
                if existing is None:
                    connection.execute(
                        "INSERT INTO project_image_analysis_current_links VALUES (?, ?, ?)",
                        (submission.image_id, analysis_id, created_at),
                    )
                else:
                    connection.execute(
                        """UPDATE project_image_analysis_current_links
                        SET project_image_analysis_version_id=?, updated_at=?
                        WHERE project_image_id=?""",
                        (analysis_id, created_at, submission.image_id),
                    )
                _confirmed_brief_context(
                    connection, workspace_id, project_id,
                    submission.brief_version_id, submission.brief_snapshot_id,
                    expected_project_lock_version,
                )
                batch.promote()
        except Exception:
            if batch is not None:
                batch.rollback()
            raise
        assert batch is not None
        batch.commit()
        return ProjectImageAnalysis(
            analysis_id, submission.status, evidence, actor_id, created_at,
        )


def _project_and_actor(connection, auth_token: str, workspace_id: str, project_id: str, permission: str):
    project = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if project is None:
        raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
    if project["workspace_id"] != workspace_id:
        raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于目标 workspace")
    return project, require_permission(connection, auth_token, workspace_id=workspace_id, project_id=project_id, permission=permission)


def _actor_id(connection, auth_token: str) -> str:
    # The command derives actor from the session, never from caller payload.
    row = connection.execute("SELECT s.actor_id FROM auth_sessions s WHERE s.token_hash=(?) AND s.revoked_at IS NULL", (sha256(auth_token.encode("utf-8")).hexdigest(),)).fetchone()
    if row is None:
        raise WorkspaceServiceError("AUTH_SESSION_INVALID", "认证会话无效")
    return str(row["actor_id"])


def _confirmed_brief_context(connection, workspace_id: str, project_id: str, brief_version_id: str, brief_snapshot_id: str, expected_lock: int, *, require_current: bool = True):
    project = connection.execute("SELECT lock_version, current_brief_snapshot_id FROM projects WHERE project_id=? AND workspace_id=?", (project_id, workspace_id)).fetchone()
    if project is None:
        raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于目标 workspace")
    if type(expected_lock) is not int or expected_lock < 0 or project["lock_version"] != expected_lock:
        raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "project lock version 不匹配")
    if require_current and project["current_brief_snapshot_id"] != brief_snapshot_id:
        raise WorkspaceServiceError("FORMAL_BRIEF_DRIFT", "当前 Brief snapshot 已变化")
    row = connection.execute(
        """SELECT v.source_document_ids_json, v.payload_ref, v.payload_sha256
           FROM brief_confirmations c JOIN brief_versions v ON v.brief_version_id=c.brief_version_id
           JOIN project_brief_snapshots s ON s.brief_snapshot_id=c.brief_snapshot_id
           WHERE c.workspace_id=? AND c.project_id=? AND c.brief_version_id=? AND c.brief_snapshot_id=?
             AND c.confirmation_status='confirmed' AND c.unresolved_conflict_count=0
             AND s.project_id=? AND s.brief_ref=v.payload_ref AND s.brief_sha256=v.payload_sha256""",
        (workspace_id, project_id, brief_version_id, brief_snapshot_id, project_id),
    ).fetchone()
    if row is None:
        raise WorkspaceServiceError("FORMAL_BRIEF_DRIFT", "当前 Brief 未确认或证据漂移")
    return row


def _project_image(value: dict) -> ProjectImage:
    return ProjectImage(
        value["image_id"], value["image_sha256"], value["links"],
        value["analysis"], value["summary"],
    )


def _validate_analysis_payload(
    value: dict, image: dict, workspace_id: str, project_id: str,
    brief_version_id: str, brief_snapshot_id: str, analysis: ProjectImageAnalysis,
    source_content: bytes,
) -> ProjectImageBusinessSummary:
    if "human_confirmation" in value:
        review = value["human_confirmation"]
        fields = {"analysis_key", "evidence_ref", "evidence_sha256", "created_by_actor_id", "created_at",
                  "status", "note", "idempotency_key", "request_sha256"}
        if (not isinstance(review, dict) or set(review) != fields
                or not all(isinstance(item, str) for item in review.values())
                or not review["analysis_key"] or not review["idempotency_key"]
                or len(review["note"]) > 1000 or not _utc_timestamp(review["created_at"])
                or value.get("reviewed_by_actor_id") != analysis.created_by_actor_id
                or value.get("reviewed_at") != analysis.created_at or not _utc_timestamp(value.get("reviewed_at"))
                or analysis.status != "pass" or value.get("status") != "pass"):
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "人工核对记录与原分析绑定不一致")
        require_sha256(review["request_sha256"], "human_confirmation.request_sha256")
        original = {key: item for key, item in value.items() if key not in {"human_confirmation", "reviewed_by_actor_id", "reviewed_at"}}
        original["status"] = review["status"]
        if original.get("analysis_origin") == "manual" or original.get("schema_version") == "mplus-project-image-analysis/v1":
            original.update(reviewed_by_actor_id=review["created_by_actor_id"], reviewed_at=review["created_at"])
        prior = ProjectImageAnalysis(review["analysis_key"], review["status"],
            ArtifactEvidence(review["evidence_ref"], review["evidence_sha256"]), review["created_by_actor_id"], review["created_at"])
        summary = _validate_analysis_payload(original, image, workspace_id, project_id,
            brief_version_id, brief_snapshot_id, prior, source_content)
        return replace(summary, evidence_status="verified")
    # Optional package provenance augments all analysis origins without changing
    # their original provider/human evidence or author timestamps.
    payload_fields = set(value) - {"package_sources"}
    origins = value.get("package_sources", [])
    if not isinstance(origins, list):
        raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片素材来源格式无效")
    for origin in origins:
        if not isinstance(origin, dict) or not {"package_id", "package_version", "media_id", "sources"} <= set(origin):
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片素材来源证据不完整")
        source_id = f"package:{origin['package_id']}@{origin['package_version']}:{origin['media_id']}"
        if (type(origin['package_version']) is not int or origin['package_version'] < 1
                or not any(link.source_document_id == source_id for link in image['links'])
                or not isinstance(origin['sources'], list) or not origin['sources']):
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片素材来源绑定漂移")
        for location in origin['sources']:
            if (not isinstance(location, dict) or not location.get('file') or not location.get('sheet')
                    or type(location.get('row')) is not int or location['row'] < 1):
                raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片素材来源位置无效")
    common = {
        "schema_version", "workspace_id", "project_id", "project_image_id",
        "image_sha256", "brief_version_id", "brief_snapshot_id", "status",
        "caption", "tags", "subjects", "scene", "purpose", "quality",
        "brand", "evidence",
    }
    schema_version = value.get("schema_version")
    manual = common | {"reviewed_by_actor_id", "reviewed_at"}
    provider = common | {
        "analysis_origin", "provider", "model", "prompt_version",
        "requested_by_actor_id", "generated_at",
    }
    v3_common = common | {
        "analysis_origin", "quality_flags", "negative_flags", "pixel_width",
        "pixel_height", "perceptual_hash",
    }
    v3_manual = v3_common | {"reviewed_by_actor_id", "reviewed_at"}
    v3_provider = v3_common | {
        "provider", "model", "prompt_version", "requested_by_actor_id", "generated_at",
    }
    v4_provider = v3_provider | {"visual_observations"}
    v4_reference = v3_common | {
        "visual_observations", "package_id", "package_version", "media_id",
        "selected_by_actor_id", "selected_at", "source_semantic_text", "visual_analysis_available",
    }
    v5_reused = v3_common | {'visual_observations', 'original_purpose',
        'rebound_by_actor_id', 'rebound_at', 'observation_source'}
    if (
        (schema_version == "mplus-project-image-analysis/v1" and payload_fields != manual)
        or (schema_version == "mplus-project-image-analysis/v2" and payload_fields != provider)
        or (
            schema_version == "mplus-project-image-analysis/v3"
            and (
                (value.get("analysis_origin") == "manual" and payload_fields != v3_manual)
                or (value.get("analysis_origin") == "provider" and payload_fields != v3_provider)
                or value.get("analysis_origin") not in {"manual", "provider"}
            )
        )
        or (
            schema_version == "mplus-project-image-analysis/v4"
            and (
                (value.get("analysis_origin") == "provider" and payload_fields != v4_provider)
                or (value.get("analysis_origin") == "source_reference" and payload_fields != v4_reference)
                or value.get("analysis_origin") not in {"provider", "source_reference"}
            )
        )
        or (schema_version == 'mplus-project-image-analysis/v5' and (
            value.get('analysis_origin') != 'reused_observation' or payload_fields != v5_reused))
        or schema_version not in {
            "mplus-project-image-analysis/v1", "mplus-project-image-analysis/v2",
            "mplus-project-image-analysis/v3", "mplus-project-image-analysis/v4", 'mplus-project-image-analysis/v5',
        }
    ):
        raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片分析 artifact 字段无效")
    if value.get("workspace_id") != workspace_id or value.get("project_id") != project_id:
        raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片分析 artifact 项目绑定漂移")
    if value.get("project_image_id") != image["image_id"] or value.get("image_sha256") != image["image_sha256"]:
        raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片分析 artifact 图片绑定漂移")
    if value.get("brief_version_id") != brief_version_id or value.get("brief_snapshot_id") != brief_snapshot_id or value.get("status") != analysis.status:
        raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片分析 artifact Brief 绑定漂移")
    if value.get('analysis_origin') == 'reused_observation':
        source = value.get('observation_source')
        common_source = {'kind', 'brief_version_id', 'brief_snapshot_id'}
        source_fields = {'analysis': common_source | {'analysis_key', 'artifact_ref', 'artifact_sha256'},
            'task_return': common_source | {'task_id', 'receipt_sha256', 'provider', 'model', 'prompt_version'}}
        if (not isinstance(source, dict) or set(source) != source_fields.get(source.get('kind'), set())
                or not all(isinstance(item, str) and item for item in source.values())
                or value.get('rebound_by_actor_id') != analysis.created_by_actor_id
                or value.get('rebound_at') != analysis.created_at or not _utc_timestamp(value.get('rebound_at'))
                or value.get('status') not in {'low_confidence', 'blocked'}
                or not isinstance(value.get('original_purpose'), str) or not value['original_purpose']
                or value.get('purpose') != '用途待按当前需求核对；原分析参考：' + value['original_purpose']):
            raise WorkspaceServiceError('IMAGE_ANALYSIS_ARTIFACT_DRIFT', '复用观察的原证据或当前需求用途绑定无效')
        if source['kind'] == 'analysis':
            ArtifactEvidence(source['artifact_ref'], source['artifact_sha256'])
        else:
            require_sha256(source['receipt_sha256'], 'receipt_sha256')
        evidence_status = 'reused_observation'
    elif value.get("analysis_origin") == "source_reference":
        source_id = f"package:{value.get('package_id')}@{value.get('package_version')}:{value.get('media_id')}"
        if (
            type(value.get("package_version")) is not int or value["package_version"] < 1
            or not any(link.source_document_id == source_id for link in image["links"])
            or value.get("selected_by_actor_id") != analysis.created_by_actor_id
            or value.get("selected_at") != analysis.created_at
            or not _utc_timestamp(value.get("selected_at"))
            or not isinstance(value.get("source_semantic_text"), str)
            or type(value.get("visual_analysis_available")) is not bool
        ):
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "参考图片采用证据漂移")
        evidence_status = "source_reference"
    elif schema_version == "mplus-project-image-analysis/v1" or value.get("analysis_origin") == "manual":
        if (
            value.get("reviewed_by_actor_id") != analysis.created_by_actor_id
            or value.get("reviewed_at") != analysis.created_at
            or not _utc_timestamp(value.get("reviewed_at"))
        ):
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片分析人工证据漂移")
        evidence_status = "verified"
    else:
        if (
            value.get("analysis_origin") != "provider"
            or value.get("requested_by_actor_id") != analysis.created_by_actor_id
            or value.get("generated_at") != analysis.created_at
            or not _utc_timestamp(value.get("generated_at"))
            or not all(
                isinstance(value.get(field), str) and value[field].strip()
                for field in ("provider", "model", "prompt_version")
            )
        ):
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片分析 Provider 证据漂移")
        evidence_status = "provider_generated"
    strings = {}
    for field in ("caption", "scene", "purpose", "quality", "brand"):
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", f"图片分析 {field} 类型无效")
        strings[field] = item.strip()
    tags = _analysis_strings(value.get("tags"), "tags")
    subjects = _analysis_strings(value.get("subjects"), "subjects")
    _analysis_strings(value.get("evidence"), "evidence")
    if schema_version in {"mplus-project-image-analysis/v3", "mplus-project-image-analysis/v4", 'mplus-project-image-analysis/v5'}:
        if (
            type(value.get("pixel_width")) is not int
            or type(value.get("pixel_height")) is not int
            or value["pixel_width"] < 1
            or value["pixel_height"] < 1
            or not isinstance(value.get("perceptual_hash"), str)
            or re.fullmatch(r"[0-9a-f]{16}", value["perceptual_hash"]) is None
        ):
            raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片像素质量证据无效")
        metrics = {
            "pixel_width": value["pixel_width"],
            "pixel_height": value["pixel_height"],
            "perceptual_hash": value["perceptual_hash"],
        }
    else:
        metrics = project_image_pixel_evidence(source_content)
    quality_flags = _analysis_flags(value.get("quality_flags", []), "quality_flags", PROJECT_IMAGE_QUALITY_FLAGS)
    negative_flags = _analysis_flags(value.get("negative_flags", []), "negative_flags", PROJECT_IMAGE_NEGATIVE_FLAGS)
    observations = value.get("visual_observations", {})
    if not isinstance(observations, dict) or observations != _visual_observations(observations):
        raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", "图片视觉观察字段无效")
    return ProjectImageBusinessSummary(
        strings["caption"], tags, subjects, strings["scene"], strings["purpose"],
        strings["quality"], strings["brand"], evidence_status,
        quality_flags, negative_flags, metrics["pixel_width"], metrics["pixel_height"],
        metrics["perceptual_hash"], observations,
        source_semantic_text=value.get("source_semantic_text", ""),
        visual_analysis_available=value.get("visual_analysis_available"),
        evidence_content_sha256=value['image_sha256'] if value.get('visual_analysis_available') is not False else '',
        evidence_prompt_version=str(value.get('prompt_version') or ''),
    )


def _visual_observations(value: dict) -> dict:
    """Keep bounded visible observations; legacy analyses have no invented values."""
    normalized = normalize_visual_analysis(value)
    strings = ("main_subject", "camera_angle", "scene_type", "shot_type", "action_type", "ocr_text")
    lists = ("foreground_subjects", "background_subjects", "uncertainties", "quality_flags", "negative_flags")
    result = {key: str(normalized[key])[:2000] for key in strings if normalized.get(key)}
    result.update({key: list(normalized[key])[:40] for key in lists if normalized.get(key)})
    return result


def _validate_visual_flags(quality_flags: tuple[str, ...], negative_flags: tuple[str, ...]) -> None:
    if (
        not isinstance(quality_flags, tuple)
        or any(item not in PROJECT_IMAGE_QUALITY_FLAGS for item in quality_flags)
        or not isinstance(negative_flags, tuple)
        or any(item not in PROJECT_IMAGE_NEGATIVE_FLAGS for item in negative_flags)
    ):
        raise WorkspaceServiceError("INVALID_INPUT", "图片质量标记无效")


def _analysis_flags(value, field: str, allowed: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or item not in allowed for item in value
    ):
        raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", f"图片分析 {field} 无效")
    return tuple(dict.fromkeys(value))


def _source_location(
    filename: str, value: dict, source_document_id: str,
) -> ProjectImageSourceLocation:
    allowed = {"page", "sheet", "row", "column", "paragraph", "slide", "image_index", "part"}
    if not value or not set(value) <= allowed:
        raise WorkspaceServiceError("INTEGRITY_ERROR", "图片来源位置字段无效")
    if (
        not isinstance(filename, str) or not filename.strip() or filename in {".", ".."}
        or Path(filename).is_absolute() or Path(filename).name != filename
        or any(character in filename for character in "/\\\x00")
        or any(unicodedata.category(character).startswith("C") for character in filename)
    ):
        raise WorkspaceServiceError("INTEGRITY_ERROR", "图片来源标签无效")
    label = filename
    page = _positive_position(value.get("page"), "page")
    row = _positive_position(value.get("row"), "row")
    column = _positive_position(value.get("column"), "column")
    paragraph = _positive_position(value.get("paragraph"), "paragraph")
    slide = _positive_position(value.get("slide"), "slide")
    image = _positive_position(value.get("image_index"), "image_index")
    sheet = value.get("sheet")
    part = value.get("part")
    if part is not None and (
        not isinstance(part, str) or not part or part.startswith("/")
        or "\\" in part or any(token in {"", ".", ".."} for token in part.split("/"))
        or any(unicodedata.category(character).startswith("C") for character in part)
        or not part.endswith(".xml")
    ):
        raise WorkspaceServiceError("INTEGRITY_ERROR", "图片来源 part 无效")
    if sheet is not None and (not isinstance(sheet, str) or not sheet.strip()):
        raise WorkspaceServiceError("INTEGRITY_ERROR", "图片来源 sheet 无效")
    if not any(item is not None for item in (page, row, slide, image, sheet, paragraph)):
        raise WorkspaceServiceError("INTEGRITY_ERROR", "图片来源位置为空")
    if not isinstance(source_document_id, str) or not source_document_id.strip():
        raise WorkspaceServiceError("INTEGRITY_ERROR", "图片来源 document 绑定无效")
    return ProjectImageSourceLocation(
        label, page, sheet.strip() if isinstance(sheet, str) else None,
        row, slide, image, source_document_id.strip(),
        column=column, paragraph=paragraph, part=part,
    )


def _employee_source(value: ProjectImageSourceLocation) -> dict:
    if not isinstance(value, ProjectImageSourceLocation):
        raise TypeError("员工来源 DTO 只接受 ProjectImageSourceLocation")
    result = {"label": value.source_label}
    for field in ("page", "sheet", "row", "column", "paragraph", "slide", "image"):
        item = getattr(value, field)
        if item is not None:
            result[field] = item
    return result


def _positive_position(value: object, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise WorkspaceServiceError("INTEGRITY_ERROR", f"图片来源 {field} 无效")
    return value


def _analysis_strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise WorkspaceServiceError("IMAGE_ANALYSIS_ARTIFACT_DRIFT", f"图片分析 {field} 类型无效")
    return tuple(item.strip() for item in value)


def _utc_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    matched = _UTC_Z.fullmatch(value)
    if matched is None:
        return False
    fraction = matched.group("fraction")
    normalized = matched.group("base")
    if fraction is not None:
        normalized += "." + fraction.ljust(6, "0")
    normalized += "+0000"
    try:
        parsed = datetime.strptime(
            normalized,
            "%Y-%m-%dT%H:%M:%S.%f%z" if fraction is not None else "%Y-%m-%dT%H:%M:%S%z",
        )
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def _json_list(value: str, field: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkspaceServiceError("INTEGRITY_ERROR", f"{field} 无法解析") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) and item for item in parsed):
        raise WorkspaceServiceError("INTEGRITY_ERROR", f"{field} 无效")
    return parsed


def _json_object(value: str, field: str) -> dict:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise WorkspaceServiceError("INTEGRITY_ERROR", f"{field} 无法解析") from exc
    if not isinstance(parsed, dict):
        raise WorkspaceServiceError("INTEGRITY_ERROR", f"{field} 无效")
    return parsed


def _canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _analysis_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _safe_image_mime(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    raise WorkspaceServiceError("IMAGE_ARTIFACT_DRIFT", "项目图片 MIME 无效")
