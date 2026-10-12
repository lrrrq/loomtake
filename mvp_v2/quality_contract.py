from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Mapping, Sequence

from .template_logic import logical_slot_index


MACHINE_PASS = "pass"
MACHINE_FAIL = "fail"
HUMAN_PENDING = "pending"
HUMAN_QUALITY_PASS = "quality_pass"
HUMAN_QUALITY_FAIL = "quality_fail"
RELEASE_BLOCKED = "blocked"
RELEASE_TRIAL_PASS = "trial_pass"
RELEASE_PASS = "release_pass"

_REVIEW_COLUMNS = ("结构审核", "叙事审核", "节奏审核", "创意审核", "配图审核", "总体审核")
_CANONICAL_KEY_ROLES = (1, 3, 10, 11, 12, 15, 16, 18)


@dataclass(frozen=True)
class QualityThresholds:
    shot_count: int = 18
    min_approved_shots: int = 15
    max_needs_revision_shots: int = 3
    max_rejected_shots: int = 0
    max_major_changes: int = 1
    required_key_slots: tuple = (1, 3, 10, 11, 12, 15, 16, 18)
    min_accepted_images: int = 17
    min_discussability_rate: float = 0.60
    min_labor_time_saving_rate: float = 0.30


def compute_review_metrics(review_rows: Sequence[Mapping]) -> dict:
    approved = 0
    needs_revision = 0
    rejected = 0
    major_changes = 0
    accepted_images = 0
    discussable = 0
    incomplete = 0
    present_slots = set()
    non_approved_slots = set()
    logical_slots = {}
    total = len(review_rows)

    for row in review_rows:
        overall = _aggregate_review_status(row)
        image = _status(_value(row, "配图审核", "image_status", "image")) or overall
        slot = _slot_index(_value(row, "slot_index", "槽位", "镜号"))
        major_change = _major_change(_value(row, "重大修改", "requires_major_change", "is_major_change"))

        if slot is not None:
            present_slots.add(slot)
            logical_slots[slot] = logical_slot_index(dict(row), total)
            if overall != "approved":
                non_approved_slots.add(slot)
        if not _is_complete(row):
            incomplete += 1
        if overall == "approved":
            approved += 1
        elif overall == "needs_revision":
            needs_revision += 1
        elif overall == "rejected":
            rejected += 1
        if image == "approved":
            accepted_images += 1
        if major_change:
            major_changes += 1
        if not major_change and overall in {"approved", "needs_revision"}:
            discussable += 1

    required_key_slots = sorted(
        slot for slot, logic_slot in logical_slots.items()
        if logic_slot in _CANONICAL_KEY_ROLES
    )
    failed_key_slots = _failed_required_slots(
        present_slots,
        non_approved_slots,
        required_key_slots,
    )
    denominator = max(total, 1)

    return {
        "total_shot_count": len(review_rows),
        "approved_shot_count": approved,
        "needs_revision_shot_count": needs_revision,
        "rejected_shot_count": rejected,
        "major_change_count": major_changes,
        "failed_key_slots": failed_key_slots,
        "accepted_image_count": accepted_images,
        "shot_pass_rate": approved / denominator,
        "image_acceptance": accepted_images / denominator,
        "discussability_rate": discussable / denominator,
        "incomplete_shot_count": incomplete,
        "_present_slots": sorted(present_slots),
        "_non_approved_slots": sorted(non_approved_slots),
        "_required_key_slots": required_key_slots,
    }


def compute_human_review_status(
    metrics: Mapping,
    thresholds: QualityThresholds | None = None,
) -> str:
    thresholds = thresholds or thresholds_for_shot_count(
        int(metrics.get("total_shot_count") or 0),
        tuple(metrics.get("_required_key_slots") or ()),
    )
    if (
        metrics["total_shot_count"] != thresholds.shot_count
        or metrics["incomplete_shot_count"] > 0
    ):
        return HUMAN_PENDING
    if set(metrics["_present_slots"]) != set(range(1, thresholds.shot_count + 1)):
        return HUMAN_QUALITY_FAIL
    failed_key_slots = _failed_required_slots(
        metrics["_present_slots"],
        metrics["_non_approved_slots"],
        thresholds.required_key_slots,
    )
    if (
        metrics["approved_shot_count"] >= thresholds.min_approved_shots
        and metrics["needs_revision_shot_count"] <= thresholds.max_needs_revision_shots
        and metrics["rejected_shot_count"] <= thresholds.max_rejected_shots
        and metrics["major_change_count"] <= thresholds.max_major_changes
        and not failed_key_slots
        and metrics["accepted_image_count"] >= thresholds.min_accepted_images
    ):
        return HUMAN_QUALITY_PASS
    return HUMAN_QUALITY_FAIL


def thresholds_for_shot_count(
    shot_count: int,
    required_key_slots: tuple = (),
) -> QualityThresholds:
    count = max(0, int(shot_count or 0))
    return QualityThresholds(
        shot_count=count,
        min_approved_shots=ceil(count * 15 / 18),
        max_needs_revision_shots=ceil(count * 3 / 18),
        required_key_slots=tuple(required_key_slots),
        min_accepted_images=ceil(count * 17 / 18),
    )


def labor_time_saving_rate(manual_active_minutes: float, assisted_active_minutes: float) -> float:
    if manual_active_minutes <= 0:
        raise ValueError("人工基线必须大于 0")
    rate = (manual_active_minutes - assisted_active_minutes) / manual_active_minutes
    return max(-1, min(1, rate))


def compute_release_decision(
    machine_status: str,
    warning_count: int,
    human_status: str,
    discussability_rate: float,
    labor_saving_rate: float,
    thresholds: QualityThresholds = QualityThresholds(),
) -> str:
    business_gate_passed = (
        discussability_rate >= thresholds.min_discussability_rate
        and labor_saving_rate >= thresholds.min_labor_time_saving_rate
    )
    if machine_status == MACHINE_PASS and warning_count == 0 and business_gate_passed:
        if human_status == HUMAN_QUALITY_PASS:
            return RELEASE_PASS
        if human_status == HUMAN_QUALITY_FAIL:
            return RELEASE_TRIAL_PASS
    return RELEASE_BLOCKED


def release_exit_code(
    machine_status: str,
    warning_count: int,
    human_status: str,
    discussability_rate: float,
    labor_saving_rate: float,
    thresholds: QualityThresholds = QualityThresholds(),
    system_error: bool = False,
    input_error: bool = False,
) -> int:
    if system_error or input_error:
        return 1
    if machine_status != MACHINE_PASS or warning_count > 0:
        return 2
    decision = compute_release_decision(
        machine_status,
        warning_count,
        human_status,
        discussability_rate,
        labor_saving_rate,
        thresholds,
    )
    if decision == RELEASE_PASS:
        return 0
    if decision == RELEASE_TRIAL_PASS:
        return 4
    return 3


def _is_complete(row: Mapping) -> bool:
    return bool(_aggregate_review_status(row))


def _aggregate_review_status(row: Mapping) -> str:
    statuses = []
    for column in _REVIEW_COLUMNS:
        value = _value(row, column)
        if not str(value or "").strip():
            continue
        status = _status(value)
        if not status:
            return ""
        statuses.append(status)
    if "rejected" in statuses:
        return "rejected"
    if "needs_revision" in statuses:
        return "needs_revision"
    if "approved" in statuses:
        return "approved"
    return ""


def _value(row: Mapping, *keys: str):
    for key in keys:
        if key in row:
            return row[key]
    return ""


def _status(value) -> str:
    text = str(value or "").strip().lower()
    if text in {"通过", "pass", "approved"}:
        return "approved"
    if text in {"需修改", "needs_revision"}:
        return "needs_revision"
    if text in {"不通过", "fail", "rejected"}:
        return "rejected"
    return ""


def _major_change(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"是", "true", "1", "yes"}
    return bool(value)


def _slot_index(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _failed_required_slots(present_slots, non_approved_slots, required_slots) -> list:
    present = set(present_slots)
    non_approved = set(non_approved_slots)
    return sorted(
        slot
        for slot in required_slots
        if slot not in present or slot in non_approved
    )
