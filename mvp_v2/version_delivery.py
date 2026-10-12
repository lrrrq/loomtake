from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from threading import RLock
from typing import Mapping

from .artifact_service import ArtifactResolutionError, ResolvedArtifact, RunArtifactResolver
from .identity_service import require_permission
from .run_contract import sha256_file, sha256_json
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    check_version,
    require_artifact_ref,
    require_sha256,
)


def require_version_ancestor(connection, project_id: str, version_id: str, ancestor_of: str) -> None:
    """Validate the frozen parent chain inside the caller's current transaction."""
    cursor, visited = ancestor_of, set()
    while True:
        if not cursor or cursor in visited:
            raise WorkspaceServiceError("INTEGRITY_ERROR", "审核来源不属于当前版本祖先链")
        visited.add(cursor)
        edge = connection.execute(
            "SELECT parent_version_id FROM project_versions WHERE version_id=? AND project_id=?",
            (cursor, project_id),
        ).fetchone()
        if edge is None:
            raise WorkspaceServiceError("INTEGRITY_ERROR", "审核来源版本不属于当前项目")
        if cursor == version_id:
            return
        cursor = str(edge["parent_version_id"] or "")


_SCHEMA_VERSION = "mplus-version-delivery-context/v1"
_STAGE_NAMES = ("script_plan", "script_draft", "visual_binding")
_EXPECTED_SCHEMAS = {
    "script_plan": {
        "mplus-script-plan/v1", "mplus-script-plan/v2",
        "mplus-script-plan/v3", "mplus-script-plan/v4",
    },
    "script_draft": {"mplus-script-draft/v1"},
    "visual_binding": {"mplus-visual-binding/v1"},
}


def _immutable_file_fingerprint(paths: tuple[Path, ...]) -> tuple:
    try:
        return tuple((str(path.resolve()), value.st_dev, value.st_ino, value.st_size,
                      value.st_mtime_ns, value.st_ctime_ns)
                     for path in paths for value in (path.stat(),))
    except OSError:
        return ()


@dataclass(frozen=True)
class VersionDeliveryArtifact:
    ref: str
    sha256: str

    def as_dict(self) -> dict[str, str]:
        return {"ref": self.ref, "sha256": self.sha256}


@dataclass(frozen=True)
class VersionDeliveryArtifacts:
    script_plan: VersionDeliveryArtifact
    script_draft: VersionDeliveryArtifact
    visual_binding: VersionDeliveryArtifact

    def as_dict(self) -> dict[str, dict[str, str]]:
        return {
            "script_plan": self.script_plan.as_dict(),
            "script_draft": self.script_draft.as_dict(),
            "visual_binding": self.visual_binding.as_dict(),
        }


@dataclass(frozen=True)
class VersionDeliveryContext:
    schema_version: str
    workspace_id: str
    project_id: str
    project_lock_version: int
    version_id: str
    run_id: str
    parent_version_id: str | None
    version_artifact: VersionDeliveryArtifact
    manifest: VersionDeliveryArtifact
    artifacts: VersionDeliveryArtifacts

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "project_lock_version": self.project_lock_version,
            "version_id": self.version_id,
            "run_id": self.run_id,
            "parent_version_id": self.parent_version_id,
            "version_artifact": self.version_artifact.as_dict(),
            "manifest": self.manifest.as_dict(),
            "artifacts": self.artifacts.as_dict(),
        }


def resolve_version_shot_ids(
    artifacts: RunArtifactResolver, context: VersionDeliveryContext,
    script_rows: list[dict] | None = None,
) -> dict[str, str]:
    """Reuse frozen revision lineage across script, image and review projections.

    This is the script workspace's existing identity rule. A saved revision must
    supply its complete lineage; only an initial version without a report uses
    the original version-scoped identity derivation.
    """
    if script_rows is None:
        resolved = artifacts.resolve_stage_artifact(
            context.run_id, "script_draft", expected_manifest_sha256=context.manifest.sha256,
        )
        script_rows = json.loads(resolved.path.read_text(encoding="utf-8"))["script_rows"]
    expected = {str(row.get("镜号") or row.get("shot_no") or "").strip() for row in script_rows}
    if "" in expected or len(expected) != len(script_rows):
        raise WorkspaceServiceError("ARTIFACT_INVALID", "版本镜号不完整或重复")
    try:
        resolved = artifacts.resolve_artifact(context.run_id, "revision_report")
    except ArtifactResolutionError as exc:
        if exc.code != "NOT_FOUND" or exc.message != "run artifact 不存在":
            raise WorkspaceServiceError("ARTIFACT_INVALID", "版本镜头身份报告不可验证") from exc
        return {shot_no: "shot_" + sha256_json({
            "project_id": context.project_id, "version_id": context.version_id, "shot_no": shot_no,
        })[:24] for shot_no in expected}
    try:
        report = json.loads(resolved.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异报告不可读取") from exc
    if report.get("child_version_id") != context.version_id:
        raise WorkspaceServiceError("ARTIFACT_INVALID", "版本镜头身份绑定不一致")
    mapping = {
        str(item.get("child_shot_no") or ""): str(item.get("child_shot_id") or "")
        for item in (report.get("version_diff") or {}).get("shot_diffs") or []
        if item.get("child_shot_no") and item.get("child_shot_id")
    }
    if set(mapping) != expected or len(set(mapping.values())) != len(mapping):
        raise WorkspaceServiceError("ARTIFACT_INVALID", "版本镜头 lineage 不完整")
    return mapping


class VersionDeliveryService:
    """Read the authenticated, immutable three-stage delivery context for a current version."""

    def __init__(self, store: WorkspaceStore, artifacts: RunArtifactResolver):
        self.store = store
        self.artifacts = artifacts
        self.store.initialize()
        self._verified_stage_cache: dict[tuple, tuple] = {}
        self._verified_stage_cache_lock = RLock()

    def resolve(
        self,
        auth_token: str,
        project_id: str,
        version_id: str,
        expected_project_version: int | None = None,
        *, ancestor_of: str | None = None, reuse_immutable: bool = False,
    ) -> VersionDeliveryContext:
        with self.store.connection() as connection:
            project = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
            require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="project.read",
            )
            if expected_project_version is not None:
                check_version(
                    project["lock_version"],
                    expected_project_version,
                    "PROJECT_VERSION_CONFLICT",
                )
            current_anchor = ancestor_of or version_id
            if project["current_version_id"] != current_anchor:
                raise WorkspaceServiceError("VERSION_NOT_CURRENT", "version 不是 project 当前版本")
            if ancestor_of:
                require_version_ancestor(connection, project_id, version_id, ancestor_of)
            version = connection.execute(
                "SELECT * FROM project_versions WHERE version_id = ? AND project_id = ?",
                (version_id, project_id),
            ).fetchone()
            if version is None:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "current version 不属于 project")
            if version["parent_version_id"] is not None:
                parent = connection.execute(
                    "SELECT 1 FROM project_versions WHERE version_id = ? AND project_id = ?",
                    (version["parent_version_id"], project_id),
                ).fetchone()
                if parent is None:
                    raise WorkspaceServiceError("INTEGRITY_ERROR", "parent version 不属于 project")
            binding = connection.execute(
                "SELECT * FROM project_runs WHERE run_id = ? AND project_id = ?",
                (version["run_id"], project_id),
            ).fetchone()
            if binding is None:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "version run 不属于 project")
            workspace_id = str(project["workspace_id"])
            project_lock_version = int(project["lock_version"])
            run_id = str(version["run_id"])
            parent_version_id = version["parent_version_id"]
            version_ref = str(version["artifact_ref"])
            version_sha256 = str(version["content_sha256"])
            manifest_ref = str(binding["manifest_ref"])
            manifest_sha256 = str(binding["manifest_sha256"])

        try:
            version_ref = require_artifact_ref(version_ref, "version_artifact.ref")
            version_sha256 = require_sha256(version_sha256, "version_artifact.sha256")
            manifest_sha256 = require_sha256(manifest_sha256, "manifest.sha256")
            expected_manifest_ref = f"artifact://run/{run_id}/manifest"
            if manifest_ref != expected_manifest_ref:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "run manifest 引用不匹配")
            manifest = self.artifacts.resolve_manifest(
                run_id, expected_sha256=manifest_sha256
            )
            cache_key = (run_id, manifest_sha256, version_ref, version_sha256)
            with self._verified_stage_cache_lock:
                cached = self._verified_stage_cache.get(cache_key) if reuse_immutable else None
            if cached and _immutable_file_fingerprint(cached[0]) == cached[1]:
                resolved = cached[2]
            else:
                version_artifact = self._require_version_artifact(run_id, version_ref, version_sha256)
                resolved = {
                    stage: self.artifacts.resolve_stage_artifact(
                        run_id, stage, expected_manifest_sha256=manifest_sha256
                    )
                    for stage in _STAGE_NAMES
                }
                verified_artifacts = (manifest, version_artifact, *(resolved[stage] for stage in _STAGE_NAMES))
                paths = tuple(item.path for item in verified_artifacts)
                before = _immutable_file_fingerprint(paths)
                if reuse_immutable:
                    # The fingerprint must describe the bytes whose hashes were checked,
                    # including a write between resolution and this first fingerprint.
                    try:
                        if any(sha256_file(item.path) != item.sha256 for item in verified_artifacts):
                            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "版本证据在读取期间已变化")
                    except OSError as exc:
                        raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "版本证据在读取期间已变化") from exc
                self._validate_stage_chain(resolved)
                if reuse_immutable:
                    if not before or _immutable_file_fingerprint(paths) != before:
                        raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "版本证据在读取期间已变化")
                    with self._verified_stage_cache_lock:
                        if len(self._verified_stage_cache) >= 32:
                            self._verified_stage_cache.pop(next(iter(self._verified_stage_cache)))
                        self._verified_stage_cache[cache_key] = (paths, before, resolved)
        except ArtifactResolutionError as exc:
            raise WorkspaceServiceError(exc.code, exc.message) from exc

        with self.store.connection() as connection:
            current_project = connection.execute(
                "SELECT workspace_id, lock_version, current_version_id FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if current_project is None:
                raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
            require_permission(
                connection,
                auth_token,
                workspace_id=current_project["workspace_id"],
                project_id=project_id,
                permission="project.read",
            )
            if current_project["current_version_id"] != current_anchor:
                raise WorkspaceServiceError("VERSION_NOT_CURRENT", "version 已不再是 project 当前版本")
            if int(current_project["lock_version"]) != project_lock_version:
                raise WorkspaceServiceError(
                    "PROJECT_VERSION_CONFLICT", "project 在 delivery 校验期间已变化"
                )
            project_lock_version = int(current_project["lock_version"])

        return VersionDeliveryContext(
            schema_version=_SCHEMA_VERSION,
            workspace_id=workspace_id,
            project_id=project_id,
            project_lock_version=project_lock_version,
            version_id=version_id,
            run_id=run_id,
            parent_version_id=parent_version_id,
            version_artifact=VersionDeliveryArtifact(version_ref, version_sha256),
            manifest=VersionDeliveryArtifact(manifest.ref, manifest.sha256),
            artifacts=VersionDeliveryArtifacts(
                **{
                    stage: VersionDeliveryArtifact(item.ref, item.sha256)
                    for stage, item in resolved.items()
                }
            ),
        )

    def _require_version_artifact(
        self, run_id: str, version_ref: str, version_sha256: str
    ) -> ResolvedArtifact:
        prefix = f"artifact://run/{run_id}/"
        if not version_ref.startswith(prefix):
            raise WorkspaceServiceError("INTEGRITY_ERROR", "version artifact 不属于 run")
        artifact_name = version_ref[len(prefix):]
        if artifact_name == "manifest":
            resolved = self.artifacts.resolve_manifest(
                run_id, expected_sha256=version_sha256
            )
        else:
            resolved = self.artifacts.resolve_artifact(run_id, artifact_name)
        if resolved.sha256 != version_sha256:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "version artifact SHA-256 已变化")
        return resolved

    @staticmethod
    def _validate_stage_chain(resolved: Mapping[str, ResolvedArtifact]) -> None:
        expected_shot_nos: list[str] | None = None
        for stage in _STAGE_NAMES:
            try:
                payload = json.loads(resolved[stage].path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkspaceServiceError("ARTIFACT_INVALID", f"{stage} 不是可读 JSON") from exc
            if not isinstance(payload, dict) or payload.get("schema_version") not in _EXPECTED_SCHEMAS[stage]:
                raise WorkspaceServiceError("ARTIFACT_INVALID", f"{stage} schema_version 不受支持")
            script_rows = payload.get("script_rows")
            detail_rows = payload.get("detail_rows")
            if not isinstance(script_rows, list) or not isinstance(detail_rows, list):
                raise WorkspaceServiceError("ARTIFACT_INVALID", f"{stage} 缺少镜头链")
            if len(script_rows) != len(detail_rows):
                raise WorkspaceServiceError("ARTIFACT_INVALID", f"{stage} script/detail 数量不一致")
            shot_nos = _ordered_shot_nos(
                script_rows, ("镜号", "shot_no"), f"{stage}.script_rows"
            )
            detail_shot_nos = _ordered_shot_nos(
                detail_rows,
                ("镜号", "shot_no", "模板镜号", "slot_index"),
                f"{stage}.detail_rows",
            )
            if detail_shot_nos != shot_nos:
                raise WorkspaceServiceError(
                    "ARTIFACT_INVALID", f"{stage} detail_rows 镜号链不一致"
                )
            if stage == "visual_binding":
                bindings = payload.get("bindings")
                if not isinstance(bindings, list):
                    raise WorkspaceServiceError(
                        "ARTIFACT_INVALID", "visual_binding.bindings 必须是 array"
                    )
                binding_shot_nos = _ordered_shot_nos(
                    bindings, ("shot_no",), "visual_binding.bindings"
                )
                if binding_shot_nos != shot_nos:
                    raise WorkspaceServiceError(
                        "ARTIFACT_INVALID", "visual_binding.bindings 镜号链不一致"
                    )
            if expected_shot_nos is None:
                expected_shot_nos = shot_nos
            elif shot_nos != expected_shot_nos:
                raise WorkspaceServiceError("ARTIFACT_INVALID", "三阶段镜头 N 链不一致")


def _ordered_shot_nos(
    rows: list,
    field_names: tuple[str, ...],
    label: str,
) -> list[str]:
    shot_nos: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise WorkspaceServiceError("ARTIFACT_INVALID", f"{label} 含非 object")
        shot_no = ""
        for field in field_names:
            value = str(row.get(field) or "").strip()
            if value:
                shot_no = value
                break
        if not shot_no or shot_no in shot_nos:
            raise WorkspaceServiceError("ARTIFACT_INVALID", f"{label} 镜号缺失或重复")
        shot_nos.append(shot_no)
    return shot_nos
