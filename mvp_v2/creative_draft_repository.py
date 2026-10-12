"""Artifact adapter for the existing revision core before a Formal base exists.

Only a project head is indexed in SQLite. Draft bodies, edit history and inputs
are immutable payloads; one authenticated transaction publishes a complete head.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json

from .formal_brief_domain import ArtifactPayload
from .formal_brief_workspace_service import FormalBriefArtifactStore
from .identity_service import require_permission
from .run_contract import sha256_json
from .version_review_service import Draft, LockSet, UndoCheckpoint, VersionReviewError
from .workspace_store import WorkspaceServiceError, utc_now


class CreativeDraftRepository:
    def __init__(self, store, *, artifact_root, auth_token, workspace_id, project_id):
        self.store = store
        self.artifacts = FormalBriefArtifactStore(artifact_root, namespace="creative-draft")
        self.auth_token, self.workspace_id, self.project_id = auth_token, workspace_id, project_id
        self.body = None

    def _read_head(self, connection, *, write=False):
        actor = require_permission(connection, self.auth_token, workspace_id=self.workspace_id,
            project_id=self.project_id, permission="project.update" if write else "project.read")
        project = connection.execute("SELECT * FROM projects WHERE project_id=? AND workspace_id=?",
            (self.project_id, self.workspace_id)).fetchone()
        if project is None:
            raise WorkspaceServiceError("NOT_FOUND", "项目不存在")
        if write and project["status"] != "active":
            raise WorkspaceServiceError("PROJECT_ARCHIVED", "项目已归档")
        head = connection.execute("SELECT * FROM creative_draft_heads WHERE project_id=? AND workspace_id=?",
            (self.project_id, self.workspace_id)).fetchone()
        return project, actor, head

    def _read(self, connection, *, write=False):
        project, actor, head = self._read_head(connection, write=write)
        body = {"schema": "mplus-creative-draft/v1", "draft": None, "history": [], "cursor": 0,
            "patches": [], "checkpoints": [], "locks": None, "inputs": [], "active_task_id": None,
            "receipts": {}}
        if head:
            body = json.loads(self.artifacts.read(ArtifactPayload(head["payload_ref"], head["payload_sha256"])))
            if body.get("schema") != "mplus-creative-draft/v1":
                raise WorkspaceServiceError("INTEGRITY_ERROR", "初稿内容格式不可读取")
        return project, actor, head, body

    def read(self):
        with self.store.connection() as connection:
            project, actor, head, body = self._read(connection)
        return {"actor_id": actor["actor_id"], "project_version": project["lock_version"], "revision": head["revision"] if head else 0,
            "payload_ref": head["payload_ref"] if head else None,
            "payload_sha256": head["payload_sha256"] if head else None, "body": body}

    def read_head(self):
        """Read the authorized immutable payload identity without opening it."""
        with self.store.connection() as connection:
            project, actor, head = self._read_head(connection)
        return {"actor_id": actor["actor_id"], "project_version": project["lock_version"],
            "revision": head["revision"] if head else 0,
            "payload_ref": head["payload_ref"] if head else None,
            "payload_sha256": head["payload_sha256"] if head else None}

    def answer_tasks(self, body):
        """Recover old answer/task links from immutable heads without rewriting history."""
        links = deepcopy(body.get("answer_tasks", {}))
        if body.get("answer_tasks_initialized") or not body.get("consumed_answers"):
            return links
        snapshots, seen = [], set()
        current = body
        while current:
            snapshots.append(current)
            ref = current.get("previous_payload_ref")
            if not ref or ref in seen or not current.get("consumed_answers"):
                break
            seen.add(ref)
            current = json.loads(self.artifacts.read(ArtifactPayload(ref, ref.rsplit("/", 1)[-1])))
        assigned = set()
        for snapshot in reversed(snapshots):
            task_id = snapshot.get("active_task_id")
            if snapshot.get("active_task_kind") != "revision" or not task_id:
                continue
            # Only the first head of each task freezes its input set. Later heads
            # may already contain answers that belong to the following task.
            if task_id in links:
                continue
            answers = [item["answer_id"] for item in snapshot.get("consumed_answers", [])
                       if item["answer_id"] not in assigned]
            links[task_id] = answers
            assigned.update(answers)
        return links

    def manual_values(self, body):
        values = deepcopy(body.get("manual_field_values", {}))
        if body.get("manual_fields_initialized") or not body.get("draft"):
            return values
        # Old drafts already store table patches and hashed undo checkpoints.
        # Resolve the stable block identity in the exact saved snapshot, never
        # by applying a historical row number to today's reordered paragraphs.
        checkpoints = {item["revision"]: item for item in body.get("checkpoints", [])}
        snapshots = {sha256_json(items): items for items in body.get("history", [])}
        current = {item["shot_id"]: item for item in body["draft"]["shots"]}
        for patch in body.get("patches", []):
            checkpoint = checkpoints.get(patch["revision"], {})
            if patch["origin"] != "table" or patch["draft_id"] != body["draft"]["draft_id"]:
                continue
            shot = next((item for item in snapshots.get(checkpoint.get("snapshot_sha256"), [])
                         if item["shot_no"] == patch["shot_no"]), None)
            if (shot and patch["field"] in shot["fields"]
                    and current.get(shot["shot_id"], {}).get("fields", {}).get(patch["field"]) == patch["new_value"]):
                values.setdefault(shot["shot_id"], {}).setdefault(patch["field"], patch["new_value"])
        return values

    @contextmanager
    def transaction(self, *, expected_project_version, expected_revision, advance_project_version=True):
        """Buffer core repository writes, publishing them together after validation."""
        if self.body is not None:
            raise RuntimeError("Creative draft transaction already active")
        with self.store.transaction() as connection:
            project, actor, head, body = self._read(connection, write=True)
            revision = head["revision"] if head else 0
            if project["lock_version"] != expected_project_version:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目已变化，请保留输入并刷新")
            if revision != expected_revision:
                raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "初稿已变化，请保留输入并刷新")
            before = deepcopy(body)
            self.body, self.actor_id = body, actor["actor_id"]
            try:
                yield self
                if self.body != before:
                    self.body["previous_payload_ref"] = head["payload_ref"] if head else None
                    payload = self.artifacts.write(json.dumps(self.body, ensure_ascii=False,
                        sort_keys=True, separators=(",", ":")).encode("utf-8"))
                    now = utc_now()
                    connection.execute("""INSERT INTO creative_draft_heads VALUES(?, ?, ?, ?, ?, ?)
                        ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,
                        payload_ref=excluded.payload_ref, payload_sha256=excluded.payload_sha256,
                        updated_at=excluded.updated_at""", (self.workspace_id, self.project_id,
                        revision + 1, payload.ref, payload.sha256, now))
                    if advance_project_version:
                        connection.execute("UPDATE projects SET lock_version=lock_version+1, updated_at=? WHERE project_id=?",
                            (now, self.project_id))
            finally:
                self.body = None

    def _state(self):
        if self.body is None:
            raise RuntimeError("Use an authenticated creative draft transaction")
        return self.body

    def save_draft(self, draft, *, history, cursor):
        body = self._state()
        if (draft.workspace_id, draft.project_id, draft.base_version_id) != (self.workspace_id, self.project_id, None):
            raise VersionReviewError("初稿不能伪造正式版本或跨项目保存")
        if body["draft"] and body["draft"]["draft_id"] != draft.draft_id:
            body.update(patches=[], checkpoints=[], locks=None, image_references=[],
                        manual_field_values={}, manual_fields_initialized=True,
                        declined_field_values={}, declined_field_answer_ids={})
        body.update(draft=asdict(draft), history=deepcopy(history), cursor=cursor)

    def draft_state(self, draft_id):
        body = self._state()
        draft = body["draft"]
        if not draft or draft["draft_id"] != draft_id:
            raise VersionReviewError("draft 不存在")
        return Draft(**{**deepcopy(draft), "shots": tuple(deepcopy(draft["shots"]))}), deepcopy(body["history"]), body["cursor"]

    def save_lock_set(self, lock_set):
        self.draft_state(lock_set.draft_id)
        self._state()["locks"] = asdict(lock_set)

    def lock_set(self, draft_id):
        self.draft_state(draft_id)
        value = self._state()["locks"]
        return LockSet(**{**value, "entries": tuple(tuple(item) for item in value["entries"])}) if value else None

    def save_draft_change_set(self, draft, *, history, cursor, patches, checkpoint):
        self.save_draft(draft, history=history, cursor=cursor)
        self._state()["patches"].extend(asdict(item) for item in patches)
        self.add_checkpoint(checkpoint)

    def add_checkpoint(self, checkpoint):
        self.draft_state(checkpoint.draft_id)
        self._state()["checkpoints"].append(asdict(checkpoint))

    def checkpoints(self, draft_id):
        self.draft_state(draft_id)
        return [UndoCheckpoint(**item) for item in self._state()["checkpoints"]]

    def add_patch(self, patch):
        self.draft_state(patch.draft_id)
        self._state()["patches"].append(asdict(patch))
