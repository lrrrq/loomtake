"""H+20 project-bound Alpha Brief integration.

This composition layer intentionally reuses the immutable Alpha Brief and
project snapshot stores.  It neither changes Workspace schema nor accepts the
legacy raw-Brief/``confirmed`` onboarding contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .alpha_brief import AlphaBriefError, AlphaBriefStore
from .brief_artifact_store import BriefArtifactStore
from .identity_service import LocalIdentityAdapter
from .project_service import ProjectService
from .workspace_store import WorkspaceServiceError


@dataclass(frozen=True)
class AlphaProjectFlow:
    """Bind one confirmed Alpha Brief to one already-created project."""

    projects: ProjectService
    identity: LocalIdentityAdapter
    alpha_briefs: AlphaBriefStore
    normalized_briefs: BriefArtifactStore

    def create_empty_project(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        name: str,
        category_id: str,
        expected_workspace_version: int,
    ) -> dict:
        return self.projects.create_project(
            auth_token,
            workspace_id=workspace_id,
            name=name,
            category_id=category_id,
            expected_workspace_version=expected_workspace_version,
        )

    def freeze_documents(self, auth_token: str, *, project_id: str, documents: list) -> dict:
        self.projects.get_project(auth_token, project_id)
        return self._alpha(self.alpha_briefs.freeze_documents, self._input_documents(documents))

    def import_documents(self, auth_token: str, *, project_id: str, documents: list) -> dict:
        self.projects.get_project(auth_token, project_id)
        return self._alpha(
            self.alpha_briefs.import_documents, project_id, self._input_documents(documents)
        )

    def resolve_conflicts(
        self,
        auth_token: str,
        *,
        project_id: str,
        imported: dict,
        resolutions: dict[str, str],
    ) -> dict:
        self.projects.get_project(auth_token, project_id)
        return self._alpha(
            self.alpha_briefs.resolve_conflicts,
            imported,
            resolutions,
            resolved_by=self._actor_id(auth_token),
        )

    def confirm_and_bind(
        self,
        auth_token: str,
        *,
        project_id: str,
        imported: dict,
        expected_project_version: int,
    ) -> dict:
        self.projects.get_project(auth_token, project_id)
        confirmed = self._alpha(
            self.alpha_briefs.confirm, imported, confirmed_by=self._actor_id(auth_token)
        )
        if confirmed["project_id"] != project_id:
            raise WorkspaceServiceError("BRIEF_PROJECT_MISMATCH", "Alpha Brief 不属于当前项目")
        normalized = self._alpha(
            self.alpha_briefs.planning_input,
            project_id,
            confirmed["brief_ref"],
            confirmed["brief_sha256"],
        )
        frozen = self.normalized_briefs.freeze(normalized)
        snapshot = self.projects.add_brief_snapshot(
            auth_token,
            project_id=project_id,
            brief_ref=frozen["brief_ref"],
            brief_sha256=frozen["brief_sha256"],
            expected_project_version=expected_project_version,
        )
        return {
            "confirmed_brief": {
                "brief_version_id": confirmed["brief_version_id"],
                "brief_ref": confirmed["brief_ref"],
                "brief_sha256": confirmed["brief_sha256"],
            },
            "normalized_brief": frozen,
            "snapshot": snapshot,
        }

    def bound_planning_input(self, auth_token: str, *, project_id: str) -> tuple[dict, dict]:
        snapshot = self.projects.get_current_brief_snapshot(auth_token, project_id)
        if snapshot is None:
            raise WorkspaceServiceError("BRIEF_ARTIFACT_UNAVAILABLE", "项目尚未绑定已确认的 Alpha Brief")
        normalized = self.normalized_briefs.read(snapshot["brief_ref"], snapshot["brief_sha256"])
        alpha_ref = str(normalized.get("Alpha Brief引用") or "")
        alpha_sha256 = str(normalized.get("Brief来源 SHA256") or "")
        expected = self._alpha(
            self.alpha_briefs.planning_input, project_id, alpha_ref, alpha_sha256
        )
        if normalized != expected:
            raise WorkspaceServiceError("BRIEF_ARTIFACT_UNAVAILABLE", "项目快照未能还原同一已确认 Alpha Brief")
        return normalized, snapshot

    @staticmethod
    def _alpha(callback, *args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except AlphaBriefError as exc:
            raise WorkspaceServiceError("ALPHA_BRIEF_UNAVAILABLE", str(exc)) from exc

    def _actor_id(self, auth_token: str) -> str:
        return str(self.identity.resolve_auth_session(auth_token)["actor_id"])

    def _input_documents(self, documents: list) -> list[dict]:
        if not isinstance(documents, list):
            raise WorkspaceServiceError("INVALID_INPUT", "documents 必须是相对 inputs 的列表")
        root = self.alpha_briefs.inputs_root.resolve()
        resolved: list[dict] = []
        for raw in documents:
            item = dict(raw) if isinstance(raw, dict) else {"path": raw}
            relative = Path(str(item.get("path") or ""))
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise WorkspaceServiceError("INVALID_INPUT", "Alpha documents 只能是相对 inputs 的路径")
            # Keep the lexical path intact so AlphaBriefStore can still reject
            # every symlink ancestor before resolving the file itself.
            item["path"] = root / relative
            resolved.append(item)
        return resolved
