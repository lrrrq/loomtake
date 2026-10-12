from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Protocol
from uuid import uuid4

from .models import parse_positive_seconds_strict


class VersionReviewError(ValueError):
    pass


def _validate_field_value(fields: dict, field: str, value: Any) -> None:
    if field != "时长":
        return
    duration = str(value if value is not None else "").strip()
    creative = "段落" in fields
    pending = creative and duration in {"", "待定"}
    numeric = re.sub(r"^建议\s*", "", duration) if creative else duration
    if not pending and (isinstance(value, bool) or parse_positive_seconds_strict(numeric) is None):
        raise VersionReviewError("时长须为大于 0 的整数秒；初稿可留空或填待定。原内容已保留。")


def _refresh_adaptation_for_edit(shot: dict, field: str, value: Any, origin: str) -> None:
    public_fields = {"类型": "camera_type", "景别": "shot_size", "运镜": "camera_move", "画面描述": "visual"}
    if field not in public_fields:
        return
    detail = shot.get("source_detail_override")
    if not isinstance(detail, dict) or not detail.get("原创改编依据"):
        return
    prior = json.loads(detail["原创改编依据"])
    if prior.get("origin") != "original_suggestion":
        return
    adaptation = {**prior, "fields": {**prior["fields"], public_fields[field]: value},
                  "previous": prior, "edit_origin": origin}
    detail["原创改编依据"] = json.dumps(adaptation, ensure_ascii=False, sort_keys=True)
    labels = {value: key for key, value in public_fields.items()}
    detail["改编说明"] = "原创拍摄建议：" + "；".join(labels[key] + "=" + str(item) for key, item in adaptation["fields"].items()) + "。依据：已确认 Brief、创作标准与本次修改；来源仅供创意参考。"
    if isinstance(shot.get("source_match_evidence"), dict):
        shot["source_match_evidence"]["adaptation"] = deepcopy(adaptation)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _copy_shots(shots: list[dict]) -> list[dict]:
    copied = deepcopy(shots)
    shot_nos = [str(item.get("shot_no") or "").strip() for item in copied]
    if not copied or not all(shot_nos) or len(shot_nos) != len(set(shot_nos)):
        raise VersionReviewError("草稿必须包含唯一且非空的 shot_no")
    for item in copied:
        if not isinstance(item.get("fields"), dict):
            raise VersionReviewError("每个镜头必须包含 fields object")
        if not isinstance(item.get("source_refs"), list) or not item["source_refs"]:
            raise VersionReviewError("每个镜头必须保留非空 source_refs")
        item["shot_no"] = str(item["shot_no"])
    return copied


@dataclass(frozen=True)
class Draft:
    draft_id: str
    workspace_id: str
    project_id: str
    base_version_id: str | None
    revision: int
    shots: tuple[dict, ...]
    status: str
    created_by_actor_id: str
    created_at: str


@dataclass(frozen=True)
class CellPatch:
    patch_id: str
    draft_id: str
    revision: int
    actor_id: str
    origin: str
    shot_no: str
    field: str
    old_value: Any
    new_value: Any
    created_at: str


@dataclass(frozen=True)
class UndoCheckpoint:
    checkpoint_id: str
    draft_id: str
    revision: int
    patch_count: int
    snapshot_sha256: str
    actor_id: str
    created_at: str


@dataclass(frozen=True)
class LockSet:
    lock_set_id: str
    draft_id: str
    entries: tuple[tuple[str, str], ...]
    created_by_actor_id: str
    created_at: str


@dataclass(frozen=True)
class FormalVersion:
    version_id: str
    workspace_id: str
    project_id: str
    parent_version_id: str
    draft_id: str
    shots: tuple[dict, ...]
    content_sha256: str
    created_by_actor_id: str
    created_at: str


@dataclass(frozen=True)
class ReviewItem:
    review_item_id: str
    workspace_id: str
    project_id: str
    version_id: str
    kind: str
    shot_no: str | None
    severity: str
    evidence_ref: str
    evidence_sha256: str
    created_at: str


@dataclass(frozen=True)
class HumanDecision:
    decision_id: str
    review_item_id: str
    version_id: str
    decision: str
    evidence_ref: str
    evidence_sha256: str
    reviewer_actor_id: str
    decided_at: str


@dataclass(frozen=True)
class FinalConfirmation:
    confirmation_id: str
    version_id: str
    decision: str
    evidence_ref: str
    evidence_sha256: str
    reviewer_actor_id: str
    confirmed_at: str


@dataclass(frozen=True)
class ShotReviewSnapshot:
    snapshot_ref: str
    snapshot_sha256: str
    total_count: int
    resolved_count: int
    unresolved_count: int
    last_reviewer_actor_id: str | None
    last_reviewed_at: str | None
    image_confirmed_shot_nos: tuple[str, ...]


class VersionReviewRepository(Protocol):
    def save_draft(self, draft: Draft, *, history: list[list[dict]], cursor: int) -> None: ...
    def draft_state(self, draft_id: str) -> tuple[Draft, list[list[dict]], int]: ...
    def save_lock_set(self, lock_set: LockSet) -> None: ...
    def lock_set(self, draft_id: str) -> LockSet | None: ...
    def add_patch(self, patch: CellPatch) -> None: ...
    def save_draft_change_set(
        self, draft: Draft, *, history: list[list[dict]], cursor: int,
        patches: list[CellPatch], checkpoint: UndoCheckpoint,
    ) -> None: ...
    def add_checkpoint(self, checkpoint: UndoCheckpoint) -> None: ...
    def checkpoints(self, draft_id: str) -> list[UndoCheckpoint]: ...
    def save_formal_version(self, version: FormalVersion) -> None: ...
    def formal_version(self, version_id: str) -> FormalVersion: ...
    def add_review_item(self, item: ReviewItem) -> None: ...
    def review_items(self, version_id: str) -> list[ReviewItem]: ...
    def add_decision(self, decision: HumanDecision) -> None: ...
    def decisions(self, version_id: str) -> list[HumanDecision]: ...
    def save_final_confirmation(self, confirmation: FinalConfirmation) -> None: ...
    def final_confirmation(self, version_id: str) -> FinalConfirmation | None: ...


class InMemoryVersionReviewRepository:
    """Anonymous fixture repository; production persistence is a later adapter."""

    def __init__(self) -> None:
        self._drafts: dict[str, tuple[Draft, list[list[dict]], int]] = {}
        self._locks: dict[str, LockSet] = {}
        self._patches: list[CellPatch] = []
        self._checkpoints: list[UndoCheckpoint] = []
        self._versions: dict[str, FormalVersion] = {}
        self._items: dict[str, list[ReviewItem]] = {}
        self._decisions: dict[str, list[HumanDecision]] = {}
        self._confirmations: dict[str, FinalConfirmation] = {}

    def save_draft(self, draft: Draft, *, history: list[list[dict]], cursor: int) -> None:
        self._drafts[draft.draft_id] = (_copy_draft(draft), deepcopy(history), cursor)

    def draft_state(self, draft_id: str) -> tuple[Draft, list[list[dict]], int]:
        try:
            draft, history, cursor = self._drafts[draft_id]
        except KeyError as exc:
            raise VersionReviewError("draft 不存在") from exc
        return _copy_draft(draft), deepcopy(history), cursor

    def save_lock_set(self, lock_set: LockSet) -> None:
        self._locks[lock_set.draft_id] = lock_set

    def lock_set(self, draft_id: str) -> LockSet | None:
        return self._locks.get(draft_id)

    def add_patch(self, patch: CellPatch) -> None:
        self._patches.append(patch)

    def save_draft_change_set(
        self, draft: Draft, *, history: list[list[dict]], cursor: int,
        patches: list[CellPatch], checkpoint: UndoCheckpoint,
    ) -> None:
        self.save_draft(draft, history=history, cursor=cursor)
        self._patches.extend(patches)
        self._checkpoints.append(checkpoint)

    def add_checkpoint(self, checkpoint: UndoCheckpoint) -> None:
        self._checkpoints.append(checkpoint)

    def checkpoints(self, draft_id: str) -> list[UndoCheckpoint]:
        return [item for item in self._checkpoints if item.draft_id == draft_id]

    def save_formal_version(self, version: FormalVersion) -> None:
        if version.version_id in self._versions:
            raise VersionReviewError("FormalVersion 不可覆盖")
        self._versions[version.version_id] = _copy_formal_version(version)

    def formal_version(self, version_id: str) -> FormalVersion:
        try:
            return _copy_formal_version(self._versions[version_id])
        except KeyError as exc:
            raise VersionReviewError("FormalVersion 不存在") from exc

    def add_review_item(self, item: ReviewItem) -> None:
        self._items.setdefault(item.version_id, []).append(item)

    def review_items(self, version_id: str) -> list[ReviewItem]:
        return list(self._items.get(version_id, []))

    def add_decision(self, decision: HumanDecision) -> None:
        self._decisions.setdefault(decision.version_id, []).append(decision)

    def decisions(self, version_id: str) -> list[HumanDecision]:
        return list(self._decisions.get(version_id, []))

    def save_final_confirmation(self, confirmation: FinalConfirmation) -> None:
        if confirmation.version_id in self._confirmations:
            raise VersionReviewError("FinalConfirmation 不可覆盖")
        self._confirmations[confirmation.version_id] = confirmation

    def final_confirmation(self, version_id: str) -> FinalConfirmation | None:
        return self._confirmations.get(version_id)


class VersionReviewService:
    """Shared table/agent draft state plus immutable human-review records.

    It deliberately has no publication, CURRENT, Agent registry or Agent-session
    dependency. A database adapter can implement VersionReviewRepository once the
    common migration owner freezes its tables.
    """

    def __init__(self, repository: VersionReviewRepository):
        self.repository = repository

    def create_draft(
        self,
        *,
        workspace_id: str,
        project_id: str,
        base_version_id: str | None,
        shots: list[dict],
        actor_id: str,
        command_id: str | None = None,
    ) -> Draft:
        snapshot = _copy_shots(shots)
        draft = Draft(
            draft_id=_command_id(command_id, "draft"), workspace_id=workspace_id, project_id=project_id,
            base_version_id=base_version_id, revision=0, shots=tuple(snapshot),
            status="open", created_by_actor_id=actor_id, created_at=_now(),
        )
        self.repository.save_draft(draft, history=[snapshot], cursor=0)
        return draft

    def set_locks(self, draft_id: str, *, entries: list[tuple[str, str]], actor_id: str) -> LockSet:
        draft, _, _ = self.repository.draft_state(draft_id)
        self._require_open(draft)
        normalized = tuple(sorted({(str(shot), str(field)) for shot, field in entries}))
        available = {str(item["shot_no"]) for item in draft.shots}
        if any(shot not in available or not field for shot, field in normalized):
            raise VersionReviewError("LockSet 包含未知镜头或空字段")
        lock_set = LockSet(_id("lock_set"), draft_id, normalized, actor_id, _now())
        self.repository.save_lock_set(lock_set)
        return lock_set

    def apply_table_patch(self, draft_id: str, *, expected_revision: int, actor_id: str, shot_no: str, field: str, value: Any, command_id: str | None = None) -> tuple[Draft, CellPatch]:
        return self._apply_patch(draft_id, expected_revision=expected_revision, actor_id=actor_id, origin="table", shot_no=shot_no, field=field, value=value, command_id=command_id)

    def apply_agent_patch(self, draft_id: str, *, expected_revision: int, actor_id: str, shot_no: str, field: str, value: Any, save_version: bool = False, formal_version_id: str | None = None, command_id: str | None = None) -> tuple[Draft, CellPatch, FormalVersion | None]:
        if save_version or formal_version_id is not None:
            raise VersionReviewError("本轮不支持 submit_draft；正式 version 必须由受控 materializer 创建")
        draft, patch = self._apply_patch(draft_id, expected_revision=expected_revision, actor_id=actor_id, origin="natural_language", shot_no=shot_no, field=field, value=value, command_id=command_id)
        return draft, patch, None

    def apply_agent_change_set(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        actor_id: str,
        operations: list[dict[str, Any]],
        command_id: str | None = None,
    ) -> tuple[Draft, list[CellPatch]]:
        """Validate every assistant edit, then persist one reversible revision atomically."""
        draft, history, cursor = self.repository.draft_state(draft_id)
        self._require_open(draft)
        self._require_revision(draft, expected_revision)
        if not operations:
            raise VersionReviewError("批量修改不能为空")
        lock_set = self.repository.lock_set(draft_id)
        locked = set(lock_set.entries if lock_set else ())
        updated = deepcopy(history[cursor])
        seen: set[tuple[str, str]] = set()
        changes: list[tuple[str, str, Any, Any]] = []
        for operation in operations:
            if not isinstance(operation, dict) or set(operation) != {"shot_no", "field", "value"}:
                raise VersionReviewError("批量修改包含未声明字段")
            shot_no = str(operation.get("shot_no") or "").strip()
            field = str(operation.get("field") or "").strip()
            target = (shot_no, field)
            if not shot_no or not field or target in seen:
                raise VersionReviewError("批量修改目标为空或重复")
            if field in {"shot_no", "source_refs"}:
                raise VersionReviewError("镜号和来源三元组不能通过编辑补丁改变")
            lock_target = (shot_no, "source_refs") if field == "__source_rebind__" else target
            if lock_target in locked:
                raise VersionReviewError("目标字段已锁定")
            shot = next((item for item in updated if item["shot_no"] == shot_no), None)
            if shot is None:
                raise VersionReviewError("补丁目标镜头或字段不存在")
            value = deepcopy(operation.get("value"))
            if field == "__source_rebind__":
                if not isinstance(value, dict) or set(value) != {
                    "source_refs", "detail_override", "evidence",
                }:
                    raise VersionReviewError("来源重匹配证据无效")
                refs = value.get("source_refs")
                detail = value.get("detail_override")
                evidence = value.get("evidence")
                if (
                    not isinstance(refs, list) or len(refs) != 1
                    or not isinstance(refs[0], dict)
                    or set(refs[0]) != {"source_file", "source_sheet", "source_row"}
                    or not str(refs[0].get("source_file") or "").strip()
                    or not str(refs[0].get("source_sheet") or "").strip()
                    or type(refs[0].get("source_row")) is not int
                    or refs[0]["source_row"] <= 0
                    or not isinstance(detail, dict)
                    or not isinstance(evidence, dict)
                    or len(str(evidence.get("source_catalog_sha256") or "")) != 64
                ):
                    raise VersionReviewError("来源重匹配证据不完整")
                old = {
                    "source_refs": deepcopy(shot.get("source_refs")),
                    "detail_override": deepcopy(shot.get("source_detail_override")),
                }
                shot["source_refs"] = deepcopy(refs)
                shot["source_detail_override"] = deepcopy(detail)
                shot["source_match_evidence"] = deepcopy(evidence)
                shot["image_reconfirmation_required"] = True
                field = "source_refs"
                target = (shot_no, field)
            else:
                if field not in shot["fields"]:
                    raise VersionReviewError("补丁目标镜头或字段不存在")
                old = deepcopy(shot["fields"][field])
                if old == value:
                    raise VersionReviewError("修改内容与当前草稿相同")
                _validate_field_value(shot["fields"], field, value)
                shot["fields"][field] = value
                _refresh_adaptation_for_edit(shot, field, value, "natural_language")
                if field in {"类型", "景别", "运镜", "画面描述"}:
                    shot["image_reconfirmation_required"] = True
            seen.add(target)
            changes.append((shot_no, field, old, value))
        actor_id = _require_actor_id(actor_id)
        history = history[: cursor + 1] + [updated]
        saved = Draft(**{
            **draft.__dict__,
            "revision": draft.revision + 1,
            "shots": tuple(deepcopy(updated)),
        })
        created_at = _now()
        patches = [
            CellPatch(
                _command_id(command_id, "cell_patch") + f"_{index}", draft_id, saved.revision, actor_id,
                "natural_language", shot_no, field, old, value, created_at,
            )
            for index, (shot_no, field, old, value) in enumerate(changes)
        ]
        checkpoint = UndoCheckpoint(
            _command_id(command_id, "undo_checkpoint"), draft_id, saved.revision, cursor + 1,
            _sha(updated), actor_id, created_at,
        )
        self.repository.save_draft_change_set(
            saved, history=history, cursor=cursor + 1,
            patches=patches, checkpoint=checkpoint,
        )
        return saved, patches

    def delete_shot(
        self, draft_id: str, *, expected_revision: int, actor_id: str,
        shot_no: str, origin: str = "natural_language", command_id: str | None = None,
    ) -> tuple[Draft, CellPatch]:
        draft, history, cursor = self.repository.draft_state(draft_id)
        self._require_open(draft)
        self._require_revision(draft, expected_revision)
        if len(draft.shots) <= 1:
            raise VersionReviewError("脚本至少必须保留一镜")
        lock_set = self.repository.lock_set(draft_id)
        if lock_set and any(entry[0] == str(shot_no) for entry in lock_set.entries):
            raise VersionReviewError("目标镜头包含已锁定字段")
        updated = deepcopy(history[cursor])
        index = next(
            (index for index, item in enumerate(updated) if item["shot_no"] == str(shot_no)),
            None,
        )
        if index is None:
            raise VersionReviewError("删除目标镜头不存在")
        removed = updated.pop(index)
        renumbered = []
        for number, item in enumerate(updated, start=1):
            previous = str(item["shot_no"])
            item["shot_no"] = str(number)
            if previous != item["shot_no"]:
                renumbered.append({"from": previous, "to": item["shot_no"]})
        history = history[: cursor + 1] + [updated]
        saved = Draft(**{
            **draft.__dict__, "revision": draft.revision + 1,
            "shots": tuple(deepcopy(updated)),
        })
        created_at = _now()
        patch = CellPatch(
            _command_id(command_id, "cell_patch"), draft_id, saved.revision, actor_id, origin,
            str(shot_no), "__delete_shot__", removed,
            {"deleted": True, "renumbered": renumbered}, created_at,
        )
        checkpoint = UndoCheckpoint(
            _command_id(command_id, "undo_checkpoint"), draft_id, saved.revision,
            cursor + 1, _sha(updated), actor_id, created_at,
        )
        self.repository.save_draft_change_set(
            saved, history=history, cursor=cursor + 1,
            patches=[patch], checkpoint=checkpoint,
        )
        return saved, patch

    def undo(self, draft_id: str, *, expected_revision: int, actor_id: str) -> Draft:
        draft, history, cursor = self.repository.draft_state(draft_id)
        self._require_open(draft)
        self._require_revision(draft, expected_revision)
        if cursor == 0:
            raise VersionReviewError("没有可撤销的变更")
        _require_actor_id(actor_id)
        return self._save_snapshot(draft, history, cursor - 1, actor_id=actor_id)

    def redo(self, draft_id: str, *, expected_revision: int, actor_id: str) -> Draft:
        draft, history, cursor = self.repository.draft_state(draft_id)
        self._require_open(draft)
        self._require_revision(draft, expected_revision)
        if cursor >= len(history) - 1:
            raise VersionReviewError("没有可恢复的变更")
        _require_actor_id(actor_id)
        return self._save_snapshot(draft, history, cursor + 1, actor_id=actor_id)

    def discard_changes(
        self, draft_id: str, *, expected_revision: int, actor_id: str,
    ) -> Draft:
        draft, history, cursor = self.repository.draft_state(draft_id)
        self._require_open(draft)
        self._require_revision(draft, expected_revision)
        if cursor == 0:
            raise VersionReviewError("没有可放弃的草稿修改")
        _require_actor_id(actor_id)
        return self._save_snapshot(draft, history, 0, actor_id=actor_id)

    def submit_draft(self, draft_id: str, *, actor_id: str, formal_version_id: str | None = None) -> FormalVersion:
        if not isinstance(self.repository, InMemoryVersionReviewRepository):
            raise VersionReviewError("本轮不支持 submit_draft；正式 version 必须由受控 materializer 创建")
        draft, history, cursor = self.repository.draft_state(draft_id)
        self._require_open(draft)
        snapshot = history[cursor]
        formal = FormalVersion(
            version_id=str(formal_version_id or _id("formal_version")).strip(),
            workspace_id=draft.workspace_id, project_id=draft.project_id,
            parent_version_id=draft.base_version_id, draft_id=draft.draft_id,
            shots=tuple(deepcopy(snapshot)), content_sha256=_sha(snapshot),
            created_by_actor_id=actor_id, created_at=_now(),
        )
        if not formal.version_id:
            raise VersionReviewError("formal_version_id 不能为空")
        sealed = Draft(**{**draft.__dict__, "status": "submitted"})
        self.repository.save_draft(sealed, history=history, cursor=cursor)
        self.repository.save_formal_version(formal)
        return formal

    def create_followup_draft(self, version_id: str, *, actor_id: str) -> Draft:
        version = self.repository.formal_version(version_id)
        return self.create_draft(workspace_id=version.workspace_id, project_id=version.project_id, base_version_id=version.version_id, shots=list(version.shots), actor_id=actor_id)

    def add_review_items(self, version_id: str, *, items: list[dict], evidence_provider=None) -> list[ReviewItem]:
        version = self.repository.formal_version(version_id)
        if self.repository.final_confirmation(version_id) is not None:
            raise VersionReviewError("已整片确认的 version 不可新增审核项")
        result = []
        valid_shots = {str(item["shot_no"]) for item in version.shots}
        for raw in items:
            shot_no = raw.get("shot_no")
            if shot_no is not None and str(shot_no) not in valid_shots:
                raise VersionReviewError("ReviewItem 引用了未知镜头")
            item_id = _id("review_item")
            evidence = evidence_provider(item_id, raw) if evidence_provider is not None else (
                str(raw.get("evidence_ref") or ""), str(raw.get("evidence_sha256") or ""), _now()
            )
            if len(evidence) != 3:
                raise VersionReviewError("ReviewItem evidence provider 必须返回引用、SHA 和服务端时间")
            evidence_ref, evidence_sha, created_at = evidence
            if not evidence_ref.startswith("artifact://") or len(evidence_sha) != 64:
                raise VersionReviewError("ReviewItem 必须包含 artifact evidence ref 和 SHA-256")
            item = ReviewItem(
                review_item_id=item_id, workspace_id=version.workspace_id,
                project_id=version.project_id, version_id=version_id,
                kind=str(raw.get("kind") or "script"), shot_no=str(shot_no) if shot_no is not None else None,
                severity=str(raw.get("severity") or "attention"), evidence_ref=evidence_ref,
                evidence_sha256=evidence_sha, created_at=created_at,
            )
            self.repository.add_review_item(item)
            result.append(item)
        return result

    def decide_review_items(self, version_id: str, *, item_ids: list[str], decision: str, evidence_ref: str, evidence_sha256: str, reviewer_actor_id: str, decided_at: str | None = None) -> list[HumanDecision]:
        if decision not in {"approve", "return"}:
            raise VersionReviewError("HumanDecision 只能 approve 或 return")
        if not evidence_ref.startswith("artifact://") or len(evidence_sha256) != 64:
            raise VersionReviewError("HumanDecision 必须包含 artifact evidence ref 和 SHA-256")
        if self.repository.final_confirmation(version_id) is not None:
            raise VersionReviewError("已整片确认的 version 不可再决策")
        items = {item.review_item_id: item for item in self.repository.review_items(version_id)}
        if not item_ids or any(item_id not in items for item_id in item_ids):
            raise VersionReviewError("HumanDecision 必须指向当前 version 的 ReviewItem")
        latest = _latest_decisions(self.repository.decisions(version_id))
        result = []
        for item_id in item_ids:
            if latest.get(item_id) is not None and latest[item_id].decision == "approve" and decision == "approve":
                raise VersionReviewError("ReviewItem 已通过，不能覆盖")
            human = HumanDecision(_id("human_decision"), item_id, version_id, decision, evidence_ref, evidence_sha256, reviewer_actor_id, decided_at or _now())
            self.repository.add_decision(human)
            result.append(human)
        return result

    @staticmethod
    def same_image_review_subject(current: dict, previous: dict, current_image: dict, previous_image: dict) -> bool:
        """Only unchanged visual intent, source and adopted-image evidence may inherit approval."""
        image_fields = ("类型", "景别", "运镜", "画面描述")
        if (any(current["fields"].get(key) != previous["fields"].get(key) for key in image_fields)
                or current.get("source_refs") != previous.get("source_refs")
                or current_image.get("decision_kind") != "image_confirmed"
                or previous_image.get("decision_kind") != "image_confirmed"):
            return False
        association_fields = {"decision_id", "shot_no", "confirmation_scope", "inherited_from_version_id"}
        return ({key: value for key, value in current_image.items() if key not in association_fields}
                == {key: value for key, value in previous_image.items() if key not in association_fields})

    def inherit_image_reviews(self, candidates: list[dict], *, evidence_authority) -> None:
        """Persist verified ancestor associations through the authenticated repository."""
        self.repository.inherit_image_reviews(candidates, evidence_authority=evidence_authority)

    def focus_recheck(self, version_id: str, *, item_ids: list[str], evidence_ref: str, evidence_sha256: str, reviewer_actor_id: str, decided_at: str | None = None) -> list[HumanDecision]:
        if self.repository.final_confirmation(version_id) is not None:
            raise VersionReviewError("已整片确认的 version 不可重点复审")
        items = {item.review_item_id: item for item in self.repository.review_items(version_id)}
        if not item_ids or not evidence_ref.startswith("artifact://") or len(evidence_sha256) != 64:
            raise VersionReviewError("重点复审必须包含 ReviewItem、人工 actor 与 artifact evidence")
        result = []
        for item_id in item_ids:
            if item_id not in items:
                raise VersionReviewError("重点复审项不属于当前 version")
            human = HumanDecision(
                _id("human_decision"), item_id, version_id, "recheck", evidence_ref,
                evidence_sha256, reviewer_actor_id, decided_at or _now(),
            )
            self.repository.add_decision(human)
            result.append(human)
        return result

    def shot_review_snapshot(self, workspace_id: str, project_id: str, version_id: str) -> ShotReviewSnapshot:
        version = self.repository.formal_version(version_id)
        if (version.workspace_id, version.project_id) != (workspace_id, project_id):
            raise VersionReviewError("quality snapshot 不能跨 workspace 或 project")
        items = self.repository.review_items(version_id)
        decisions = self.repository.decisions(version_id)
        latest = _latest_decisions(decisions)
        approved_images = {
            str(item.shot_no)
            for item in items
            if item.kind == "image"
            and item.shot_no
            and latest.get(item.review_item_id)
            and latest[item.review_item_id].decision == "approve"
        }
        unresolved_items = [
            item for item in items
            if latest.get(item.review_item_id) is None
            or latest[item.review_item_id].decision != "approve"
        ]
        unresolved_by_shot = {
            str(item.shot_no) for item in unresolved_items if item.shot_no is not None
        }
        all_shots = {str(item["shot_no"]) for item in version.shots}
        resolved_shots = {
            shot_no for shot_no in all_shots
            if shot_no in approved_images and shot_no not in unresolved_by_shot
        }
        unresolved_whole_items = sum(1 for item in unresolved_items if item.shot_no is None)
        last = decisions[-1] if decisions else None
        payload = {
            "version_id": version_id,
            "items": [item.__dict__ for item in items],
            "decisions": [item.__dict__ for item in decisions],
        }
        digest = _sha(payload)
        return ShotReviewSnapshot(
            snapshot_ref=f"memory://version/{version_id}/shot-review/{digest}",
            snapshot_sha256=digest, total_count=len(all_shots),
            resolved_count=len(resolved_shots),
            unresolved_count=len(all_shots) - len(resolved_shots) + unresolved_whole_items,
            last_reviewer_actor_id=last.reviewer_actor_id if last else None,
            last_reviewed_at=last.decided_at if last else None,
            image_confirmed_shot_nos=tuple(sorted(approved_images)),
        )

    def final_confirm(self, version_id: str, *, decision: str, evidence_ref: str, evidence_sha256: str, reviewer_actor_id: str) -> FinalConfirmation:
        confirmation = self.build_final_confirmation(
            version_id, decision=decision, evidence_ref=evidence_ref,
            evidence_sha256=evidence_sha256, reviewer_actor_id=reviewer_actor_id,
        )
        self.repository.save_final_confirmation(confirmation)
        return confirmation

    def build_final_confirmation(
        self, version_id: str, *, decision: str, evidence_ref: str,
        evidence_sha256: str, reviewer_actor_id: str,
    ) -> FinalConfirmation:
        if decision not in {"quality_pass", "quality_fail"}:
            raise VersionReviewError("FinalConfirmation 只能是 quality_pass 或 quality_fail")
        version = self.repository.formal_version(version_id)
        snapshot = self.shot_review_snapshot(version.workspace_id, version.project_id, version_id)
        all_shots = {str(item["shot_no"]) for item in version.shots}
        if decision == "quality_pass" and (
            snapshot.unresolved_count
            or set(snapshot.image_confirmed_shot_nos) != all_shots
        ):
            raise VersionReviewError("未完成全部镜头图片确认或仍有未解决审核项，不能整片 quality_pass")
        if not evidence_ref.startswith("artifact://") or len(evidence_sha256) != 64:
            raise VersionReviewError("FinalConfirmation 必须包含 artifact evidence ref 和 SHA-256")
        return FinalConfirmation(
            _id("final_confirmation"), version_id, decision, evidence_ref,
            evidence_sha256, reviewer_actor_id, _now(),
        )

    def _apply_patch(self, draft_id: str, *, expected_revision: int, actor_id: str, origin: str, shot_no: str, field: str, value: Any, command_id: str | None = None) -> tuple[Draft, CellPatch]:
        draft, history, cursor = self.repository.draft_state(draft_id)
        self._require_open(draft)
        self._require_revision(draft, expected_revision)
        if field in {"shot_no", "source_refs"}:
            raise VersionReviewError("镜号和来源三元组不能通过编辑补丁改变")
        lock_set = self.repository.lock_set(draft_id)
        if lock_set and (str(shot_no), str(field)) in lock_set.entries:
            raise VersionReviewError("目标字段已锁定")
        updated = deepcopy(history[cursor])
        shot = next((item for item in updated if item["shot_no"] == str(shot_no)), None)
        if shot is None or field not in shot["fields"]:
            raise VersionReviewError("补丁目标镜头或字段不存在")
        _validate_field_value(shot["fields"], field, value)
        old = deepcopy(shot["fields"][field])
        shot["fields"][field] = deepcopy(value)
        _refresh_adaptation_for_edit(shot, field, value, origin)
        if field in {"类型", "景别", "运镜", "画面描述"}:
            shot["image_reconfirmation_required"] = True
        actor_id = _require_actor_id(actor_id)
        history = history[: cursor + 1] + [updated]
        saved = Draft(**{
            **draft.__dict__, "revision": draft.revision + 1,
            "shots": tuple(deepcopy(updated)),
        })
        created_at = _now()
        patch = CellPatch(
            _command_id(command_id, "cell_patch"), draft_id, saved.revision,
            actor_id, origin, str(shot_no), str(field), old, deepcopy(value), created_at,
        )
        checkpoint = UndoCheckpoint(
            _command_id(command_id, "undo_checkpoint"), draft_id, saved.revision,
            cursor + 1, _sha(updated), actor_id, created_at,
        )
        self.repository.save_draft_change_set(
            saved, history=history, cursor=cursor + 1,
            patches=[patch], checkpoint=checkpoint,
        )
        return saved, patch

    def _save_snapshot(self, draft: Draft, history: list[list[dict]], cursor: int, *, actor_id: str) -> Draft:
        actor_id = _require_actor_id(actor_id)
        updated = Draft(**{**draft.__dict__, "revision": draft.revision + 1, "shots": tuple(deepcopy(history[cursor]))})
        checkpoint = UndoCheckpoint(
            _id("undo_checkpoint"), updated.draft_id, updated.revision, cursor,
            _sha(history[cursor]), actor_id, _now(),
        )
        self.repository.save_draft(updated, history=history, cursor=cursor)
        self.repository.add_checkpoint(checkpoint)
        return updated

    @staticmethod
    def _require_open(draft: Draft) -> None:
        if draft.status != "open":
            raise VersionReviewError("已提交草稿不可覆盖；请从正式版本创建新草稿")

    @staticmethod
    def _require_revision(draft: Draft, expected_revision: int) -> None:
        if draft.revision != expected_revision:
            raise VersionReviewError("DRAFT_REVISION_CONFLICT")


def _copy_draft(value: Draft) -> Draft:
    return Draft(**{**value.__dict__, "shots": tuple(deepcopy(value.shots))})


def _command_id(command_id: str | None, prefix: str) -> str:
    if command_id:
        digest = hashlib.sha256(str(command_id).encode("utf-8")).hexdigest()
        return f"{prefix}_{digest}"
    return _id(prefix)


def _copy_formal_version(value: FormalVersion) -> FormalVersion:
    return FormalVersion(**{**value.__dict__, "shots": tuple(deepcopy(value.shots))})


def _latest_decisions(decisions: list[HumanDecision]) -> dict[str, HumanDecision]:
    return {decision.review_item_id: decision for decision in decisions}


def _require_actor_id(actor_id: str) -> str:
    normalized = str(actor_id or "").strip()
    if not normalized:
        raise VersionReviewError("actor_id 不能为空")
    return normalized
