"""WorkspaceStore adapter for the T07 draft and human-review domain."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from .version_delivery import require_version_ancestor
from .identity_service import require_permission
from .version_review_service import (
    CellPatch,
    Draft,
    FormalVersion,
    HumanDecision,
    LockSet,
    ReviewItem,
    UndoCheckpoint,
    VersionReviewError,
)
from .workspace_store import WorkspaceServiceError, WorkspaceStore, new_id, utc_now


def unsaved_script_draft_id(
    connection,
    *,
    workspace_id: str,
    project_id: str,
    base_version_id: str,
) -> str | None:
    """Return the one changed, unsubmitted draft for an immutable base version."""
    rows = connection.execute(
        """SELECT DISTINCT d.draft_id
        FROM version_review_drafts d
        JOIN version_review_draft_heads h ON h.draft_id=d.draft_id
        JOIN version_review_draft_states base
          ON base.draft_id=d.draft_id AND base.parent_state_id IS NULL
        JOIN version_review_draft_states current
          ON current.draft_id=d.draft_id AND current.state_id=h.current_state_id
        LEFT JOIN version_review_draft_submissions s ON s.draft_id=d.draft_id
        WHERE d.workspace_id=? AND d.project_id=? AND d.base_version_id=?
          AND s.submission_id IS NULL AND current.snapshot_sha256<>base.snapshot_sha256
        ORDER BY d.created_at, d.draft_id""",
        (workspace_id, project_id, base_version_id),
    ).fetchall()
    if len(rows) > 1:
        raise WorkspaceServiceError("INTEGRITY_ERROR", "当前脚本存在多个未保存草稿")
    return str(rows[0]["draft_id"]) if rows else None


def open_script_draft_id(
    connection,
    *,
    workspace_id: str,
    project_id: str,
    base_version_id: str,
) -> str | None:
    """Return the one open script draft for a Formal base version."""
    rows = connection.execute(
        """SELECT d.draft_id
        FROM version_review_drafts d
        LEFT JOIN version_review_draft_submissions s ON s.draft_id=d.draft_id
        WHERE d.workspace_id=? AND d.project_id=? AND d.base_version_id=?
          AND s.submission_id IS NULL
        ORDER BY d.created_at, d.draft_id""",
        (workspace_id, project_id, base_version_id),
    ).fetchall()
    if len(rows) > 1:
        raise WorkspaceServiceError("INTEGRITY_ERROR", "当前脚本存在多个未保存草稿")
    return str(rows[0]["draft_id"]) if rows else None


def require_no_unsaved_script_draft(
    connection,
    *,
    workspace_id: str,
    project_id: str,
    base_version_id: str,
) -> None:
    if unsaved_script_draft_id(
        connection,
        workspace_id=workspace_id,
        project_id=project_id,
        base_version_id=base_version_id,
    ):
        raise WorkspaceServiceError(
            "SCRIPT_DRAFT_NOT_SAVED",
            "当前脚本有未保存修改，请先在“脚本与版本”保存为新版本或放弃修改。",
        )


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _opaque_key(kind: str, internal_id: str) -> str:
    if kind == "review":
        return "review_" + hashlib.sha256(
            f"mplus-review-item-key/v1:{internal_id}".encode("utf-8")
        ).hexdigest()[:32]
    return f"{kind}_" + hashlib.sha256(
        f"mplus-version-review-key/v1:{kind}:{internal_id}".encode("utf-8")
    ).hexdigest()[:32]


def _shots(value: Any) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        raise VersionReviewError("镜头快照必须是 array")
    result = deepcopy(list(value))
    shot_nos = [str(item.get("shot_no") or "").strip() for item in result if isinstance(item, dict)]
    if len(shot_nos) != len(result) or not all(shot_nos) or len(set(shot_nos)) != len(shot_nos):
        raise VersionReviewError("草稿必须包含唯一且非空的 shot_no")
    for item in result:
        if not isinstance(item.get("fields"), dict) or not isinstance(item.get("source_refs"), list) or not item["source_refs"]:
            raise VersionReviewError("镜头快照缺少 fields 或 source_refs")
        item["shot_no"] = str(item["shot_no"])
    return result


class WorkspaceVersionReviewRepository:
    """Authenticated persistence adapter. Actor/context are never supplied by callers."""

    def __init__(
        self, store: WorkspaceStore, *, auth_token: str, workspace_id: str,
        project_id: str, version_id: str, run_id: str,
        expected_project_lock_version: int, enforce_single_script_draft: bool = False,
        read_ancestor_of: str | None = None,
    ):
        self.read_ancestor_of = read_ancestor_of
        self.store = store
        self.auth_token = auth_token
        self.workspace_id = workspace_id
        self.project_id = project_id
        self.version_id = version_id
        self.run_id = run_id
        self.expected_project_lock_version = expected_project_lock_version
        self.enforce_single_script_draft = enforce_single_script_draft
        self.store.initialize()

    def register_subject(
        self, shots: list[dict], *, permission: str = "review.record",
    ) -> FormalVersion:
        normalized = _shots(shots)
        with self.store.transaction() as connection:
            actor = self._write_context(connection, permission=permission)
            row = connection.execute(
                "SELECT shots_json, shots_sha256, created_by_actor_id, created_at "
                "FROM version_review_subjects WHERE version_id=?",
                (self.version_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO version_review_subjects VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.version_id, self.workspace_id, self.project_id, self.run_id,
                     _canonical(normalized), _sha(normalized), actor["actor_id"], utc_now()),
                )
            elif str(row["shots_sha256"]) != _sha(normalized):
                raise WorkspaceServiceError("INTEGRITY_ERROR", "同一正式 version 的镜头快照不一致")
        return self.formal_version(self.version_id)

    def save_draft(self, draft: Draft, *, history: list[list[dict]], cursor: int) -> None:
        normalized_history = [_shots(item) for item in history]
        if not 0 <= cursor < len(normalized_history):
            raise VersionReviewError("draft history cursor 无效")
        current = normalized_history[cursor]
        if _sha(current) != _sha(list(draft.shots)):
            raise VersionReviewError("draft 当前镜头与 history 不一致")
        with self.store.transaction() as connection:
            actor = self._write_context(connection, permission="project.update")
            row = connection.execute(
                "SELECT current_revision FROM version_review_drafts WHERE draft_id=?",
                (draft.draft_id,),
            ).fetchone()
            if row is None:
                if self.enforce_single_script_draft:
                    existing = open_script_draft_id(
                        connection,
                        workspace_id=self.workspace_id,
                        project_id=self.project_id,
                        base_version_id=self.version_id,
                    )
                    if existing is not None and existing != draft.draft_id:
                        raise WorkspaceServiceError(
                            "SCRIPT_DRAFT_ALREADY_EXISTS", "当前脚本已有未保存草稿",
                        )
                self._require_draft_scope(draft)
                state_id = new_id("review_state")
                connection.execute(
                    "INSERT INTO version_review_drafts VALUES (?, ?, ?, ?, 'open', 0, ?, ?)",
                    (draft.draft_id, draft.workspace_id, draft.project_id,
                     draft.base_version_id, actor["actor_id"], draft.created_at),
                )
                connection.execute(
                    "INSERT INTO version_review_draft_states VALUES (?, ?, NULL, ?, ?, ?, ?)",
                    (state_id, draft.draft_id, _canonical(current), _sha(current),
                     actor["actor_id"], draft.created_at),
                )
                connection.execute(
                    "INSERT INTO version_review_draft_heads VALUES (?, ?, '[]', '[]', ?)",
                    (draft.draft_id, state_id, draft.created_at),
                )
                return
            if int(row["current_revision"]) != draft.revision - 1:
                raise VersionReviewError("DRAFT_REVISION_CONFLICT")
            head = connection.execute(
                "SELECT * FROM version_review_draft_heads WHERE draft_id=?", (draft.draft_id,)
            ).fetchone()
            if head is None:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "draft head 缺失")
            current_state = str(head["current_state_id"])
            undo_ids = json.loads(head["undo_state_ids_json"])
            redo_ids = json.loads(head["redo_state_ids_json"])
            state_id = self._locate_existing_state(
                connection, draft.draft_id, current, current_state, undo_ids, redo_ids
            )
            if state_id == current_state:
                raise VersionReviewError("DRAFT_REVISION_CONFLICT")
            if state_id in redo_ids:
                redo_ids.remove(state_id)
                undo_ids.append(current_state)
            elif state_id in undo_ids:
                target_index = undo_ids.index(state_id)
                redo_ids = redo_ids + [current_state] + list(reversed(undo_ids[target_index + 1:]))
                undo_ids = undo_ids[:target_index]
            else:
                state_id = new_id("review_state")
                connection.execute(
                    "INSERT INTO version_review_draft_states VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (state_id, draft.draft_id, current_state, _canonical(current), _sha(current),
                     actor["actor_id"], draft.created_at),
                )
                undo_ids.append(current_state)
                redo_ids = []
            updated = connection.execute(
                """UPDATE version_review_drafts SET current_revision=?
                WHERE draft_id=? AND current_revision=?""",
                (draft.revision, draft.draft_id, draft.revision - 1),
            )
            if updated.rowcount != 1:
                raise VersionReviewError("DRAFT_REVISION_CONFLICT")
            connection.execute(
                """UPDATE version_review_draft_heads
                SET current_state_id=?, undo_state_ids_json=?, redo_state_ids_json=?, updated_at=?
                WHERE draft_id=?""",
                (state_id, _canonical(undo_ids), _canonical(redo_ids), draft.created_at, draft.draft_id),
            )

    def draft_state(self, draft_id: str) -> tuple[Draft, list[list[dict]], int]:
        with self.store.connection() as connection:
            self._read_context(connection)
            draft = connection.execute(
                "SELECT * FROM version_review_drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
            head = connection.execute(
                "SELECT * FROM version_review_draft_heads WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if draft is None or head is None:
                raise VersionReviewError("draft 不存在")
            if (draft["workspace_id"], draft["project_id"]) != (self.workspace_id, self.project_id):
                raise WorkspaceServiceError("PERMISSION_DENIED", "draft 不属于当前 project")
            state_ids = json.loads(head["undo_state_ids_json"]) + [head["current_state_id"]] + list(reversed(json.loads(head["redo_state_ids_json"])))
            rows = {
                row["state_id"]: row for row in connection.execute(
                    "SELECT state_id, shots_json FROM version_review_draft_states WHERE draft_id=?",
                    (draft_id,),
                )
            }
        history = [_shots(json.loads(rows[state_id]["shots_json"])) for state_id in state_ids]
        return Draft(
            draft_id=str(draft["draft_id"]), workspace_id=str(draft["workspace_id"]),
            project_id=str(draft["project_id"]), base_version_id=str(draft["base_version_id"]),
            revision=int(draft["current_revision"]), shots=tuple(deepcopy(history[len(json.loads(head["undo_state_ids_json"]))])),
            status=str(draft["status"]), created_by_actor_id=str(draft["created_by_actor_id"]),
            created_at=str(draft["created_at"]),
        ), history, len(json.loads(head["undo_state_ids_json"]))

    def save_lock_set(self, lock_set: LockSet) -> None:
        with self.store.transaction() as connection:
            actor = self._write_context(connection, permission="project.update")
            self._draft_row(connection, lock_set.draft_id)
            connection.execute(
                "INSERT INTO version_review_lock_sets VALUES (?, ?, ?, ?)",
                (lock_set.lock_set_id, lock_set.draft_id, actor["actor_id"], lock_set.created_at),
            )
            for shot_no, field_name in lock_set.entries:
                connection.execute(
                    "INSERT INTO version_review_lock_entries VALUES (?, ?, ?)",
                    (lock_set.lock_set_id, shot_no, field_name),
                )
            existing = connection.execute(
                "SELECT 1 FROM version_review_lock_links WHERE draft_id=?", (lock_set.draft_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO version_review_lock_links VALUES (?, ?, ?)",
                    (lock_set.draft_id, lock_set.lock_set_id, lock_set.created_at),
                )
            else:
                connection.execute(
                    "UPDATE version_review_lock_links SET lock_set_id=?, updated_at=? WHERE draft_id=?",
                    (lock_set.lock_set_id, lock_set.created_at, lock_set.draft_id),
                )

    def lock_set(self, draft_id: str) -> LockSet | None:
        with self.store.connection() as connection:
            self._read_context(connection)
            self._draft_row(connection, draft_id)
            row = connection.execute(
                """SELECT s.* FROM version_review_lock_links l
                JOIN version_review_lock_sets s ON s.lock_set_id=l.lock_set_id
                WHERE l.draft_id=?""",
                (draft_id,),
            ).fetchone()
            if row is None:
                return None
            entries = connection.execute(
                "SELECT shot_no, field_name FROM version_review_lock_entries WHERE lock_set_id=? ORDER BY shot_no, field_name",
                (row["lock_set_id"],),
            ).fetchall()
        return LockSet(
            lock_set_id=str(row["lock_set_id"]), draft_id=draft_id,
            entries=tuple((str(item["shot_no"]), str(item["field_name"])) for item in entries),
            created_by_actor_id=str(row["created_by_actor_id"]), created_at=str(row["created_at"]),
        )

    def add_patch(self, patch: CellPatch) -> None:
        with self.store.transaction() as connection:
            actor = self._write_context(connection, permission="project.update")
            self._draft_row(connection, patch.draft_id)
            connection.execute(
                "INSERT INTO version_review_cell_patches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (patch.patch_id, patch.draft_id, patch.revision, actor["actor_id"], patch.origin,
                 patch.shot_no, patch.field, _canonical(patch.old_value), _canonical(patch.new_value),
                 patch.created_at),
            )

    def save_draft_change_set(
        self, draft: Draft, *, history: list[list[dict]], cursor: int,
        patches: list[CellPatch], checkpoint: UndoCheckpoint,
    ) -> None:
        normalized_history = [_shots(item) for item in history]
        if not 0 <= cursor < len(normalized_history):
            raise VersionReviewError("draft history cursor 无效")
        current = normalized_history[cursor]
        if _sha(current) != _sha(list(draft.shots)):
            raise VersionReviewError("draft 当前镜头与 history 不一致")
        if not patches or any(
            item.draft_id != draft.draft_id or item.revision != draft.revision
            for item in patches
        ):
            raise VersionReviewError("批量 patch 与 draft revision 不一致")
        if (
            checkpoint.draft_id != draft.draft_id
            or checkpoint.revision != draft.revision
            or checkpoint.snapshot_sha256 != _sha(current)
        ):
            raise VersionReviewError("批量 checkpoint 与 draft revision 不一致")
        with self.store.transaction() as connection:
            actor = self._write_context(connection, permission="project.update")
            row = connection.execute(
                "SELECT current_revision FROM version_review_drafts WHERE draft_id=?",
                (draft.draft_id,),
            ).fetchone()
            if row is None or int(row["current_revision"]) != draft.revision - 1:
                raise VersionReviewError("DRAFT_REVISION_CONFLICT")
            head = connection.execute(
                "SELECT * FROM version_review_draft_heads WHERE draft_id=?", (draft.draft_id,),
            ).fetchone()
            if head is None:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "draft head 缺失")
            current_state = str(head["current_state_id"])
            state_id = new_id("review_state")
            connection.execute(
                "INSERT INTO version_review_draft_states VALUES (?, ?, ?, ?, ?, ?, ?)",
                (state_id, draft.draft_id, current_state, _canonical(current), _sha(current),
                 actor["actor_id"], draft.created_at),
            )
            undo_ids = json.loads(head["undo_state_ids_json"])
            undo_ids.append(current_state)
            updated = connection.execute(
                """UPDATE version_review_drafts SET current_revision=?
                WHERE draft_id=? AND current_revision=?""",
                (draft.revision, draft.draft_id, draft.revision - 1),
            )
            if updated.rowcount != 1:
                raise VersionReviewError("DRAFT_REVISION_CONFLICT")
            connection.execute(
                """UPDATE version_review_draft_heads
                SET current_state_id=?, undo_state_ids_json=?, redo_state_ids_json='[]', updated_at=?
                WHERE draft_id=?""",
                (state_id, _canonical(undo_ids), draft.created_at, draft.draft_id),
            )
            connection.executemany(
                "INSERT INTO version_review_cell_patches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item.patch_id, item.draft_id, item.revision, actor["actor_id"], item.origin,
                        item.shot_no, item.field, _canonical(item.old_value),
                        _canonical(item.new_value), item.created_at,
                    )
                    for item in patches
                ],
            )
            connection.execute(
                "INSERT INTO version_review_undo_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    checkpoint.checkpoint_id, checkpoint.draft_id, checkpoint.revision,
                    checkpoint.patch_count, checkpoint.snapshot_sha256,
                    actor["actor_id"], checkpoint.created_at,
                ),
            )

    def add_checkpoint(self, checkpoint: UndoCheckpoint) -> None:
        with self.store.transaction() as connection:
            actor = self._write_context(connection, permission="project.update")
            self._draft_row(connection, checkpoint.draft_id)
            connection.execute(
                "INSERT INTO version_review_undo_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
                (checkpoint.checkpoint_id, checkpoint.draft_id, checkpoint.revision,
                 checkpoint.patch_count, checkpoint.snapshot_sha256, actor["actor_id"],
                 checkpoint.created_at),
            )

    def checkpoints(self, draft_id: str) -> list[UndoCheckpoint]:
        with self.store.connection() as connection:
            self._read_context(connection)
            self._draft_row(connection, draft_id)
            rows = connection.execute(
                "SELECT * FROM version_review_undo_checkpoints WHERE draft_id=? ORDER BY revision, created_at",
                (draft_id,),
            ).fetchall()
        return [UndoCheckpoint(
            checkpoint_id=str(row["checkpoint_id"]), draft_id=draft_id,
            revision=int(row["revision"]), patch_count=int(row["patch_count"]),
            snapshot_sha256=str(row["snapshot_sha256"]), actor_id=str(row["actor_id"]),
            created_at=str(row["created_at"]),
        ) for row in rows]

    def save_formal_version(self, version: FormalVersion) -> None:
        raise VersionReviewError("本轮不支持 submit_draft；正式 version 必须由受控 materializer 创建")

    def formal_version(self, version_id: str) -> FormalVersion:
        with self.store.connection() as connection:
            self._read_context(connection)
            row = connection.execute(
                "SELECT * FROM version_review_subjects WHERE version_id=?", (version_id,)
            ).fetchone()
            if row is None:
                raise VersionReviewError("FormalVersion 不存在；请先从当前 VersionDeliveryContext 注册 subject")
            if (row["workspace_id"], row["project_id"], row["run_id"]) != (
                self.workspace_id, self.project_id, self.run_id
            ):
                raise WorkspaceServiceError("PERMISSION_DENIED", "FormalVersion 不属于当前上下文")
        return FormalVersion(
            version_id=version_id, workspace_id=self.workspace_id, project_id=self.project_id,
            parent_version_id="", draft_id="", shots=tuple(_shots(json.loads(row["shots_json"]))),
            content_sha256=str(row["shots_sha256"]), created_by_actor_id=str(row["created_by_actor_id"]),
            created_at=str(row["created_at"]),
        )

    def add_review_item(self, item: ReviewItem) -> None:
        with self.store.transaction() as connection:
            actor = self._write_context(connection, permission="review.record")
            self._subject_row(connection, item.version_id)
            self._save_evidence(
                connection, actor_id=actor["actor_id"], version_id=item.version_id,
                evidence_ref=item.evidence_ref, evidence_sha256=item.evidence_sha256,
                summary=f"{item.kind}:{item.severity}",
            )
            connection.execute(
                "INSERT INTO version_review_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item.review_item_id, self.workspace_id, self.project_id, item.version_id,
                 item.kind, item.shot_no, item.severity, item.evidence_ref,
                 item.evidence_sha256, item.created_at),
            )

    def review_items(self, version_id: str) -> list[ReviewItem]:
        with self.store.connection() as connection:
            self._read_context(connection)
            self._subject_row(connection, version_id)
            rows = connection.execute(
                "SELECT * FROM version_review_items WHERE version_id=? ORDER BY created_at, review_item_id",
                (version_id,),
            ).fetchall()
        return [ReviewItem(
            review_item_id=str(row["review_item_id"]), workspace_id=self.workspace_id,
            project_id=self.project_id, version_id=version_id, kind=str(row["kind"]),
            shot_no=str(row["shot_no"]) if row["shot_no"] is not None else None,
            severity=str(row["severity"]), evidence_ref=str(row["evidence_ref"]),
            evidence_sha256=str(row["evidence_sha256"]), created_at=str(row["created_at"]),
        ) for row in rows]

    def add_decision(self, decision: HumanDecision) -> None:
        with self.store.transaction() as connection:
            actor = self._write_context(connection, permission="review.record")
            self._subject_row(connection, decision.version_id)
            self._save_evidence(
                connection, actor_id=actor["actor_id"], version_id=decision.version_id,
                evidence_ref=decision.evidence_ref, evidence_sha256=decision.evidence_sha256,
                summary=f"human:{decision.decision}",
            )
            connection.execute(
                "INSERT INTO version_review_human_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (decision.decision_id, decision.review_item_id, decision.version_id,
                 decision.decision, decision.evidence_ref, decision.evidence_sha256,
                 actor["actor_id"], decision.decided_at),
            )

    def decisions(self, version_id: str) -> list[HumanDecision]:
        with self.store.connection() as connection:
            self._read_context(connection)
            self._subject_row(connection, version_id)
            rows = connection.execute(
                "SELECT * FROM version_review_human_decisions WHERE version_id=? ORDER BY decided_at, decision_id",
                (version_id,),
            ).fetchall()
        return [HumanDecision(
            decision_id=str(row["decision_id"]), review_item_id=str(row["review_item_id"]),
            version_id=version_id, decision=str(row["decision"]),
            evidence_ref=str(row["evidence_ref"]), evidence_sha256=str(row["evidence_sha256"]),
            reviewer_actor_id=str(row["reviewer_actor_id"]), decided_at=str(row["decided_at"]),
        ) for row in rows]

    def ancestor_image_reviews(self, version_id: str) -> list[dict]:
        with self.store.connection() as connection:
            self._read_context(connection)
            self._require_ancestor(connection, version_id)
            rows = connection.execute(
                """SELECT i.review_item_id, i.shot_no, i.version_id, i.evidence_ref AS item_evidence_ref,
                i.evidence_sha256 AS item_evidence_sha256, i.created_at AS item_created_at,
                d.decision_id, d.decision, d.evidence_ref, d.evidence_sha256,
                d.reviewer_actor_id, d.decided_at, v.run_id
                FROM version_review_items i JOIN project_versions v ON v.version_id=i.version_id
                LEFT JOIN version_review_human_decisions d ON d.review_item_id=i.review_item_id
                WHERE i.version_id=? AND i.kind='image'
                ORDER BY d.decided_at, d.decision_id""", (version_id,),
            ).fetchall()
        latest = {str(row["review_item_id"]): dict(row) for row in rows}
        return list(latest.values())

    def _require_ancestor(self, connection, version_id: str) -> None:
        cursor, visited = self.version_id, set()
        while cursor:
            if cursor in visited:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "审核来源版本链循环")
            visited.add(cursor)
            row = connection.execute("SELECT parent_version_id FROM project_versions WHERE version_id=? AND project_id=?",
                (cursor, self.project_id)).fetchone()
            if row is None:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "审核来源版本不属于当前项目")
            if cursor == version_id and cursor != self.version_id:
                return
            cursor = str(row["parent_version_id"] or "")
        raise WorkspaceServiceError("INTEGRITY_ERROR", "审核来源不属于当前版本祖先链")

    def inherit_image_reviews(self, candidates: list[dict], *, evidence_authority) -> None:
        # No employee command accepts these records. Formal's frozen lineage and
        # VersionReviewService's image-subject comparison select them server-side.
        if not candidates:
            return
        context = {key: getattr(self, key) for key in ("workspace_id", "project_id", "version_id", "run_id")}
        with self.store.transaction() as connection:
            self._write_context(connection, permission="review.record")
            self._subject_row(connection, self.version_id)
            if connection.execute("SELECT 1 FROM version_quality_final_confirmations WHERE version_id=?", (self.version_id,)).fetchone():
                raise WorkspaceServiceError("REVIEW_INVALID", "已整片确认版本不能新增继承决定")
            pending = []
            for candidate in candidates:
                source = candidate["source"]
                target_id = candidate["review_item_id"]
                self._require_ancestor(connection, source["version_id"])
                target = connection.execute("SELECT kind FROM version_review_items WHERE review_item_id=? AND version_id=?",
                    (target_id, self.version_id)).fetchone()
                if target is None or target["kind"] != "image":
                    raise WorkspaceServiceError("REVIEW_INVALID", "审核继承只能关联当前版本图片项")
                if connection.execute("SELECT 1 FROM version_review_human_decisions WHERE review_item_id=?", (target_id,)).fetchone():
                    continue
                original = connection.execute("""SELECT d.*, i.kind, i.version_id AS item_version_id FROM version_review_human_decisions d
                    JOIN version_review_items i ON i.review_item_id=d.review_item_id
                    WHERE d.review_item_id=? ORDER BY d.decided_at DESC, d.decision_id DESC LIMIT 1""",
                    (source["review_item_id"],)).fetchone()
                if (original is None or original["kind"] != "image" or original["decision"] != "approve"
                        or original["item_version_id"] != source["version_id"]
                        or any(original[key] != source[key] for key in ("decision_id", "evidence_ref", "evidence_sha256", "reviewer_actor_id", "decided_at"))):
                    raise WorkspaceServiceError("REVIEW_INVALID", "原版本审核决定已变化或无法验证")
                evidence_authority.verify_existing(source, ref=source["item_evidence_ref"], sha256=source["item_evidence_sha256"],
                    kind="review-item", review_item_id=source["review_item_id"], occurred_at=source["item_created_at"])
                artifact = evidence_authority.materialize_inherited(context, review_item_id=target_id, source=source)
                pending.append((target_id, source, artifact))
            for target_id, source, artifact in pending:
                self._save_evidence(connection, actor_id=source["reviewer_actor_id"], version_id=self.version_id,
                    evidence_ref=artifact.ref, evidence_sha256=artifact.sha256, summary="未变图片沿用原版本审核证据")
                connection.execute("INSERT INTO version_review_human_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (new_id("human_decision"), target_id, self.version_id, "approve", artifact.ref, artifact.sha256,
                     source["reviewer_actor_id"], source["decided_at"]))

    def save_final_confirmation(self, confirmation) -> None:
        if confirmation.version_id != self.version_id:
            raise WorkspaceServiceError("PERMISSION_DENIED", "FinalConfirmation 不属于当前 version")
        # The following migration-9 write is the sole persisted fact. This method
        # keeps the shared domain protocol compatible with its in-memory fixture.
        return None

    def final_confirmation(self, version_id: str):
        from .version_review_service import FinalConfirmation
        with self.store.connection() as connection:
            self._read_context(connection)
            row = connection.execute(
                """SELECT * FROM version_quality_final_confirmations
                WHERE version_id=? AND workspace_id=? AND project_id=? AND run_id=?""",
                (version_id, self.workspace_id, self.project_id, self.run_id),
            ).fetchone()
        if row is None:
            return None
        return FinalConfirmation(
            confirmation_id=str(row["final_confirmation_id"]), version_id=version_id,
            decision=str(row["decision"]), evidence_ref=str(row["confirmation_ref"]),
            evidence_sha256=str(row["confirmation_sha256"]),
            reviewer_actor_id=str(row["reviewer_actor_id"]),
            confirmed_at=str(row["confirmed_at"]),
        )

    def read_models(self, version_id: str, *, evidence_authority=None) -> dict[str, list[dict] | dict | None]:
        """Employee business DTOs: no actor IDs, artifact refs/SHA/CURRENT/publication."""
        items = self.review_items(version_id)
        decisions = self.decisions(version_id)
        latest = {item.review_item_id: item for item in decisions}
        with self.store.connection() as connection:
            self._read_context(connection)
            evidence = {
                str(row["evidence_sha256"]): row for row in connection.execute(
                    "SELECT evidence_sha256, summary, created_at FROM version_review_evidence WHERE version_id=?",
                    (version_id,),
                )
            }
        issue_views = [{
            "issue_key": _opaque_key("review", item.review_item_id), "kind": item.kind,
            "shot_no": item.shot_no, "severity": item.severity,
            "status": latest[item.review_item_id].decision if item.review_item_id in latest else "open",
        } for item in items]
        if evidence_authority is not None:
            context = {key: getattr(self, key) for key in ("workspace_id", "project_id", "version_id", "run_id")}
            for item, view in zip(items, issue_views):
                decision = latest.get(item.review_item_id)
                registered = evidence.get(decision.evidence_sha256) if decision else None
                if registered is None or registered["summary"] != "未变图片沿用原版本审核证据":
                    continue
                payload = evidence_authority.verify_existing(context, ref=decision.evidence_ref,
                    sha256=decision.evidence_sha256, kind="human-decision", review_item_id=item.review_item_id,
                    decision="approve", actor_id=decision.reviewer_actor_id, occurred_at=decision.decided_at)
                source = payload["inherited_from"]
                with self.store.connection() as connection:
                    row = connection.execute("SELECT display_name FROM actors WHERE actor_id=?", (decision.reviewer_actor_id,)).fetchone()
                view["inherited_review"] = {"version_label": source["version_label"],
                    "reviewer": str(row["display_name"]) if row else "原版本审核人", "reviewed_at": decision.decided_at}
        decision_views = [{
            "decision_key": _opaque_key("decision", item.decision_id),
            "issue_key": _opaque_key("review", item.review_item_id),
            "decision": item.decision, "reviewer_label": "已由有权限人员处理",
            "decided_at": item.decided_at,
            "evidence_status": "available" if item.evidence_sha256 in evidence else "missing",
        } for item in decisions]
        evidence_views = [{
            "summary": str(row["summary"]), "created_at": str(row["created_at"]),
            "evidence_status": "available",
        } for row in evidence.values()]
        final = self.final_confirmation(version_id)
        final_view = None if final is None else {
            "decision": final.decision, "reviewer_label": "已由有权限人员处理",
            "confirmed_at": final.confirmed_at, "evidence_status": "available",
        }
        return {
            "issues": issue_views, "decisions": decision_views,
            "evidence": evidence_views, "final_confirmation": final_view,
        }

    def _write_context(self, connection, *, permission: str) -> dict:
        actor = require_permission(
            connection, self.auth_token, workspace_id=self.workspace_id,
            project_id=self.project_id, permission=permission,
        )
        self._current_context(connection)
        return actor

    def _read_context(self, connection) -> None:
        require_permission(
            connection, self.auth_token, workspace_id=self.workspace_id,
            project_id=self.project_id, permission="project.read",
        )
        self._current_context(connection, ancestor_of=self.read_ancestor_of)

    def _current_context(self, connection, *, ancestor_of: str | None = None) -> None:
        row = connection.execute(
            """SELECT p.lock_version, p.current_version_id
            FROM projects p JOIN project_versions v ON v.project_id=p.project_id
            JOIN project_runs r ON r.run_id=v.run_id AND r.project_id=p.project_id
            WHERE p.workspace_id=? AND p.project_id=? AND v.version_id=? AND r.run_id=?""",
            (self.workspace_id, self.project_id, self.version_id, self.run_id),
        ).fetchone()
        if row is None:
            raise WorkspaceServiceError("INTEGRITY_ERROR", "workspace/project/version/run 绑定不一致")
        if row["current_version_id"] != (ancestor_of or self.version_id):
            raise WorkspaceServiceError("VERSION_NOT_CURRENT", "version 不是当前 version")
        if ancestor_of:
            require_version_ancestor(connection, self.project_id, self.version_id, ancestor_of)
        if int(row["lock_version"]) != self.expected_project_lock_version:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "project lock version 不匹配")

    def _draft_row(self, connection, draft_id: str):
        row = connection.execute(
            "SELECT * FROM version_review_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise VersionReviewError("draft 不存在")
        if (row["workspace_id"], row["project_id"], row["base_version_id"]) != (
            self.workspace_id, self.project_id, self.version_id
        ):
            raise WorkspaceServiceError("PERMISSION_DENIED", "draft 不属于当前 version")
        return row

    def _subject_row(self, connection, version_id: str):
        row = connection.execute(
            "SELECT 1 FROM version_review_subjects WHERE version_id=? AND workspace_id=? AND project_id=? AND run_id=?",
            (version_id, self.workspace_id, self.project_id, self.run_id),
        ).fetchone()
        if row is None:
            raise WorkspaceServiceError("NOT_FOUND", "version review subject 不存在")
        return row

    def _require_draft_scope(self, draft: Draft) -> None:
        if (draft.workspace_id, draft.project_id, draft.base_version_id) != (
            self.workspace_id, self.project_id, self.version_id
        ):
            raise WorkspaceServiceError("PERMISSION_DENIED", "draft 上下文不匹配")

    def _save_evidence(
        self, connection, *, actor_id: str, version_id: str, evidence_ref: str,
        evidence_sha256: str, summary: str,
    ) -> None:
        row = connection.execute(
            "SELECT evidence_ref FROM version_review_evidence WHERE evidence_sha256=?",
            (evidence_sha256,),
        ).fetchone()
        if row is not None:
            if row["evidence_ref"] != evidence_ref:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "证据 SHA 与引用不一致")
            return
        connection.execute(
            "INSERT INTO version_review_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("review_evidence"), self.workspace_id, self.project_id, version_id,
             evidence_ref, evidence_sha256, str(summary).strip() or "review evidence",
             actor_id, utc_now()),
        )

    @staticmethod
    def _locate_existing_state(connection, draft_id, shots, current_state, undo_ids, redo_ids) -> str | None:
        digest = _sha(shots)
        for state_id in [current_state, *reversed(undo_ids), *redo_ids]:
            row = connection.execute(
                "SELECT snapshot_sha256 FROM version_review_draft_states WHERE state_id=? AND draft_id=?",
                (state_id, draft_id),
            ).fetchone()
            if row is not None and row["snapshot_sha256"] == digest:
                return str(state_id)
        return None
