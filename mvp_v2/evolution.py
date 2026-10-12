from __future__ import annotations

from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from .run_state import append_jsonl_ledger


EVOLUTION_LEDGER_NAME = "evolution_ledger.jsonl"
RULE_CANDIDATE_DECISIONS = {"approved", "rejected"}


def record_evolution_event(
    cache_dir: str | Path,
    *,
    run_id: str,
    event: str,
    details: dict | None = None,
) -> dict:
    payload_details = dict(details or {})
    identity = json.dumps(
        {"run_id": run_id, "event": event, "details": payload_details},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    path = Path(cache_dir) / EVOLUTION_LEDGER_NAME
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                if (json.loads(line).get("details") or {}).get("event_id") == event_id:
                    return {"event_id": event_id, "deduplicated": True, "path": str(path)}
            except json.JSONDecodeError:
                continue
    append_jsonl_ledger(
        path,
        run_id=run_id,
        event=event,
        details={"event_id": event_id, **payload_details},
    )
    return {
        "event_id": event_id,
        "deduplicated": False,
        "path": str(path),
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def summarize_evolution_ledgers(paths) -> dict:
    events = _load_evolution_events(paths)
    event_counts = Counter(str(row.get("event") or "") for row in events)
    feedback_categories = Counter(
        str((row.get("details") or {}).get("category") or "")
        for row in events
        if row.get("event") == "feedback_received"
    )
    latest_feedback_status = {}
    for row in events:
        if row.get("event") != "feedback_status_changed":
            continue
        details = row.get("details") or {}
        feedback_id = str(details.get("feedback_id") or "")
        if feedback_id:
            latest_feedback_status[feedback_id] = str(details.get("status") or "")
    feedback_statuses = Counter(latest_feedback_status.values())
    ai_outputs = event_counts.get("ai_output_created", 0)
    adopted = event_counts.get("version_adopted", 0)
    rule_candidates = build_rule_candidates(events)
    feedback_resolution = _feedback_resolution_metrics(events)
    return {
        "event_count": len(events),
        "run_count": len({str(row.get("run_id") or "") for row in events if row.get("run_id")}),
        "event_counts": dict(sorted(event_counts.items())),
        "feedback_categories": {
            key: value for key, value in sorted(feedback_categories.items()) if key
        },
        "feedback_statuses": {
            key: value for key, value in sorted(feedback_statuses.items()) if key
        },
        "ai_output_count": ai_outputs,
        "adopted_version_count": adopted,
        "adoption_rate": round(adopted / ai_outputs, 4) if ai_outputs else 0.0,
        "feedback_resolution": feedback_resolution,
        "rule_candidate_count": len(rule_candidates),
        "pending_rule_candidate_count": sum(
            candidate["decision"] == "pending" for candidate in rule_candidates
        ),
        "rule_candidates": rule_candidates,
        "automatic_rule_update": False,
    }


def build_rule_candidates(events: list[dict]) -> list[dict]:
    latest_status = {}
    for row in events:
        if row.get("event") != "feedback_status_changed":
            continue
        details = row.get("details") or {}
        feedback_id = str(details.get("feedback_id") or "").strip()
        if feedback_id:
            latest_status[feedback_id] = {
                "status": str(details.get("status") or "").strip(),
                "child_run_id": str(details.get("child_run_id") or "").strip(),
            }
    adopted_runs = {
        str(row.get("run_id") or "").strip()
        for row in events
        if row.get("event") == "version_adopted"
    }
    decisions = {}
    for row in events:
        if row.get("event") != "rule_candidate_decision":
            continue
        details = row.get("details") or {}
        candidate_id = str(details.get("candidate_id") or "").strip()
        if candidate_id:
            decisions[candidate_id] = {
                "decision": str(details.get("decision") or "pending"),
                "reviewer": str(details.get("reviewer") or ""),
                "note": str(details.get("note") or ""),
                "decided_at": str(row.get("timestamp") or ""),
            }

    groups = {}
    for row in events:
        if row.get("event") != "feedback_received":
            continue
        details = row.get("details") or {}
        feedback_id = str(details.get("feedback_id") or "").strip()
        category = str(details.get("category") or "").strip().casefold()
        reason = str(details.get("reason") or "").strip()
        normalized_reason = _normalize_rule_issue(reason)
        status = latest_status.get(feedback_id) or {}
        child_run_id = str(status.get("child_run_id") or "")
        if (
            not feedback_id
            or not category
            or not normalized_reason
            or status.get("status") != "resolved"
            or child_run_id not in adopted_runs
        ):
            continue
        key = (category, normalized_reason)
        groups.setdefault(key, []).append({
            "feedback_id": feedback_id,
            "project_id": str(row.get("_project_id") or ""),
            "parent_run_id": str(row.get("run_id") or ""),
            "child_run_id": child_run_id,
            "shot_no": str(details.get("shot_no") or ""),
            "reason": reason,
        })

    candidates = []
    for (category, normalized_reason), occurrences in sorted(groups.items()):
        project_ids = sorted({item["project_id"] for item in occurrences if item["project_id"]})
        if len(occurrences) < 2 or len(project_ids) < 2:
            continue
        identity = json.dumps(
            {"category": category, "normalized_reason": normalized_reason},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        candidate_id = "rule_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        decision = decisions.get(candidate_id) or {
            "decision": "pending",
            "reviewer": "",
            "note": "",
            "decided_at": "",
        }
        candidates.append({
            "candidate_id": candidate_id,
            "category": category,
            "issue": occurrences[0]["reason"],
            "normalized_issue": normalized_reason,
            "occurrence_count": len(occurrences),
            "project_count": len(project_ids),
            "project_ids": project_ids,
            "evidence": occurrences,
            "eligible": True,
            **decision,
            "automatic_rule_update": False,
        })
    return candidates


def record_rule_candidate_decision(
    cache_dir: str | Path,
    *,
    candidate: dict,
    decision: str,
    reviewer: str,
    note: str = "",
) -> dict:
    normalized_decision = str(decision).strip().casefold()
    normalized_reviewer = str(reviewer).strip()
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    if normalized_decision not in RULE_CANDIDATE_DECISIONS:
        raise ValueError(f"未知规则候选结论: {decision}")
    if not normalized_reviewer:
        raise ValueError("规则候选结论必须填写审核人")
    if not candidate_id or not candidate.get("eligible"):
        raise ValueError("只能审核具备跨项目采用证据的规则候选")
    return record_evolution_event(
        cache_dir,
        run_id="governance",
        event="rule_candidate_decision",
        details={
            "candidate_id": candidate_id,
            "decision": normalized_decision,
            "reviewer": normalized_reviewer,
            "note": str(note).strip(),
            "evidence_feedback_ids": sorted({
                str(item.get("feedback_id") or "")
                for item in candidate.get("evidence") or []
                if item.get("feedback_id")
            }),
            "automatic_rule_update": False,
        },
    )


def _load_evolution_events(paths) -> list[dict]:
    events = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event"):
                events.append({**row, "_project_id": path.parent.name})
    return events


def _normalize_rule_issue(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    normalized = re.sub(r"[\s，。！？、；;：:,.!?]+", "", normalized)
    return normalized


def _feedback_resolution_metrics(events: list[dict]) -> dict:
    received_at = {}
    resolved_at = {}
    for row in events:
        details = row.get("details") or {}
        feedback_id = str(details.get("feedback_id") or "").strip()
        if not feedback_id:
            continue
        if row.get("event") == "feedback_received":
            received_at[feedback_id] = _parse_timestamp(row.get("timestamp"))
        elif (
            row.get("event") == "feedback_status_changed"
            and details.get("status") == "resolved"
        ):
            resolved_at[feedback_id] = _parse_timestamp(row.get("timestamp"))
    durations = sorted(
        (resolved_at[feedback_id] - started).total_seconds()
        for feedback_id, started in received_at.items()
        if started and resolved_at.get(feedback_id) and resolved_at[feedback_id] >= started
    )
    return {
        "resolved_count": len(durations),
        "average_seconds": round(sum(durations) / len(durations), 3) if durations else 0.0,
        "median_seconds": round(
            durations[len(durations) // 2]
            if len(durations) % 2
            else (durations[len(durations) // 2 - 1] + durations[len(durations) // 2]) / 2,
            3,
        ) if durations else 0.0,
    }


def _parse_timestamp(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
