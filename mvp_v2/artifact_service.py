from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .portable_paths import resolve_run_local_path


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_ARTIFACT_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class ArtifactResolutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ResolvedArtifact:
    ref: str
    path: Path
    sha256: str
    size_bytes: int


class RunArtifactResolver:
    """Resolve authenticated run artifacts and their content-verified frozen source."""

    def __init__(self, runs_root: str | Path):
        self.runs_root = Path(runs_root).expanduser().resolve()

    def read_manifest(
        self,
        run_id: str,
        *,
        expected_sha256: str = "",
    ) -> dict:
        manifest = self.resolve_manifest(run_id, expected_sha256=expected_sha256)
        try:
            payload = json.loads(manifest.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactResolutionError(
                "ARTIFACT_INVALID", "run manifest 无法读取"
            ) from exc
        if str(payload.get("run_id") or "") != run_id:
            raise ArtifactResolutionError(
                "ARTIFACT_ID_MISMATCH", "目录 run_id 与 manifest 不一致"
            )
        return payload

    def resolve_manifest(
        self,
        run_id: str,
        *,
        expected_sha256: str = "",
    ) -> ResolvedArtifact:
        run_id = self._validate_run_id(run_id)
        candidates = [
            path.resolve()
            for path in self.runs_root.glob(f"*/runs/{run_id}/run_manifest.json")
            if path.is_file()
        ]
        if not candidates:
            raise ArtifactResolutionError("NOT_FOUND", "run manifest 不存在")
        if len(candidates) != 1:
            raise ArtifactResolutionError(
                "ARTIFACT_AMBIGUOUS", "同一 run_id 对应多个 manifest"
            )
        path = candidates[0]
        self._require_inside_root(path)
        digest = _sha256(path)
        if expected_sha256 and digest != expected_sha256.lower():
            raise ArtifactResolutionError(
                "ARTIFACT_HASH_MISMATCH", "run manifest SHA-256 已变化"
            )
        return ResolvedArtifact(
            ref=f"artifact://run/{run_id}/manifest",
            path=path,
            sha256=digest,
            size_bytes=path.stat().st_size,
        )

    def resolve_template_workbook(
        self, run_id: str, *, expected_manifest_sha256: str,
    ) -> Path:
        """Validate the external source frozen by an authenticated parent run."""
        payload = self.read_manifest(run_id, expected_sha256=expected_manifest_sha256)
        record = payload.get("template_workbook")
        if not isinstance(record, dict):
            raise ArtifactResolutionError("ARTIFACT_INVALID", "父运行缺少冻结工作簿来源")
        raw_path = str(record.get("path") or "").strip()
        expected = str(record.get("sha256") or "").strip().lower()
        if not raw_path or not Path(raw_path).is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ArtifactResolutionError("ARTIFACT_INVALID", "冻结工作簿缺少完整路径或 SHA-256")
        path = Path(raw_path).resolve()
        try:
            digest = _sha256(path)
        except OSError as exc:
            raise ArtifactResolutionError("NOT_FOUND", "冻结工作簿文件不可读取") from exc
        if digest != expected:
            raise ArtifactResolutionError("ARTIFACT_HASH_MISMATCH", "冻结工作簿内容已变化")
        return path

    def resolve_artifact(self, run_id: str, artifact_name: str) -> ResolvedArtifact:
        run_id = self._validate_run_id(run_id)
        if not _ARTIFACT_NAME.fullmatch(str(artifact_name or "")):
            raise ArtifactResolutionError("INVALID_INPUT", "artifact_name 无效")
        manifest_ref = self.resolve_manifest(run_id)
        payload = self.read_manifest(run_id, expected_sha256=manifest_ref.sha256)
        record = (payload.get("artifacts") or {}).get(artifact_name)
        if not isinstance(record, dict):
            raise ArtifactResolutionError("NOT_FOUND", "run artifact 不存在")
        raw_path = str(record.get("path") or "").strip()
        expected_sha256 = str(record.get("sha256") or "").strip().lower()
        if not raw_path or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ArtifactResolutionError(
                "ARTIFACT_INVALID", "artifact 记录缺少路径或 SHA-256"
            )
        run_dir = manifest_ref.path.parent.resolve()
        path = resolve_run_local_path(run_dir, raw_path)
        try:
            path.relative_to(run_dir)
        except ValueError as exc:
            raise ArtifactResolutionError(
                "ARTIFACT_SCOPE_VIOLATION", "artifact 不在所属 run 目录"
            ) from exc
        if not path.is_file():
            raise ArtifactResolutionError("NOT_FOUND", "run artifact 文件不存在")
        digest = _sha256(path)
        if digest != expected_sha256:
            raise ArtifactResolutionError(
                "ARTIFACT_HASH_MISMATCH", "run artifact SHA-256 已变化"
            )
        return ResolvedArtifact(
            ref=f"artifact://run/{run_id}/{artifact_name}",
            path=path,
            sha256=digest,
            size_bytes=path.stat().st_size,
        )

    def resolve_stage_artifact(
        self,
        run_id: str,
        stage_id: str,
        *,
        expected_manifest_sha256: str = "",
    ) -> ResolvedArtifact:
        run_id = self._validate_run_id(run_id)
        if not _ARTIFACT_NAME.fullmatch(str(stage_id or "")):
            raise ArtifactResolutionError("INVALID_INPUT", "stage_id 无效")
        manifest = self.resolve_manifest(
            run_id, expected_sha256=expected_manifest_sha256
        )
        payload = self.read_manifest(run_id, expected_sha256=manifest.sha256)
        record = (payload.get("stages") or {}).get(stage_id)
        if not isinstance(record, dict) or record.get("status") not in {
            "succeeded",
            "succeeded_with_warnings",
        }:
            raise ArtifactResolutionError("NOT_FOUND", f"{stage_id} 未形成成功阶段产物")
        refs = record.get("output_refs")
        expected_sha256 = str(record.get("output_sha256") or "").strip().lower()
        if (
            not isinstance(refs, list)
            or len(refs) != 1
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        ):
            raise ArtifactResolutionError(
                "ARTIFACT_INVALID", f"{stage_id} 阶段缺少唯一产物或 SHA-256"
            )
        run_dir = manifest.path.parent.resolve()
        path = resolve_run_local_path(run_dir, refs[0])
        try:
            path.relative_to(run_dir)
        except ValueError as exc:
            raise ArtifactResolutionError(
                "ARTIFACT_SCOPE_VIOLATION", f"{stage_id} 阶段产物不在所属 run"
            ) from exc
        if path.name != f"{stage_id}.json" or not path.is_file():
            raise ArtifactResolutionError("NOT_FOUND", f"{stage_id} 阶段产物文件不存在")
        digest = _sha256(path)
        if digest != expected_sha256:
            raise ArtifactResolutionError(
                "ARTIFACT_HASH_MISMATCH", f"{stage_id} 阶段产物 SHA-256 已变化"
            )
        return ResolvedArtifact(
            ref=f"artifact://run/{run_id}/{stage_id}",
            path=path,
            sha256=digest,
            size_bytes=path.stat().st_size,
        )

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        normalized = str(run_id or "").strip()
        if not _RUN_ID.fullmatch(normalized) or Path(normalized).name != normalized:
            raise ArtifactResolutionError("INVALID_INPUT", "run_id 无效")
        return normalized

    def _require_inside_root(self, path: Path) -> None:
        try:
            path.relative_to(self.runs_root)
        except ValueError as exc:
            raise ArtifactResolutionError(
                "ARTIFACT_SCOPE_VIOLATION", "manifest 不在配置运行目录"
            ) from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
