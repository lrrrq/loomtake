from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from typing import Iterable


VERSION_DIFF_SCHEMA_VERSION = "mplus-version-diff/v1"
REVISION_REPORT_SCHEMA_VERSION = "mplus-revision-report/v1"
LINEAGE_RELATIONS = (
    "preserved",
    "moved",
    "split_from",
    "merged_from",
    "new",
    "deleted",
)

_SHOT_METADATA_FIELDS = {
    "shot_id",
    "shot_no",
    "fields",
    "source_refs",
    "identity_key",
}
_SOURCE_FIELD_ALIASES = {"source", "sources", "source_refs", "来源", "来源三元组"}


def compare_versions(
    *,
    project_id: str,
    parent_version_id: str,
    child_version_id: str,
    parent_shots: list[dict],
    child_shots: list[dict],
) -> dict:
    """Return a deterministic version/shot/field diff.

    The function deliberately accepts and returns plain JSON-compatible data.
    Storage, workspace identities and publication state remain the caller's
    responsibility. A child only inherits a parent ``shot_id`` when lineage is
    uniquely proven by an explicit id, identity key, source triple set or (when
    both sides have no sources) identical field content.
    """
    project = _required_id(project_id, "project_id")
    parent_version = _required_id(parent_version_id, "parent_version_id")
    child_version = _required_id(child_version_id, "child_version_id")
    parents = _normalize_shots(project, parent_version, parent_shots)
    children = _normalize_shots(project, child_version, child_shots)

    relationships, review_items = _resolve_lineage(parents, children)
    for relation, parent_index, child_index in relationships:
        if relation not in {"preserved", "moved"}:
            continue
        children[child_index]["shot_id"] = parents[parent_index]["shot_id"]

    shot_diffs = [
        _shot_diff(relation, parents, children, parent_index, child_index)
        for relation, parent_index, child_index in relationships
    ]
    relation_counts = Counter(item["relation"] for item in shot_diffs)
    if review_items:
        relation_counts["lineage_needs_review"] = len(review_items)
    changed_field_count = sum(
        item["status"] != "unchanged"
        for shot in shot_diffs
        for item in shot["field_diffs"]
    )
    changed_child_ids = {
        item["child_shot_id"]
        for item in shot_diffs
        if item["child_shot_id"]
        and (
            item["relation"] not in {"preserved", "moved"}
            or any(field["status"] != "unchanged" for field in item["field_diffs"])
            or item["position_changed"]
        )
    }
    deleted_parent_ids = {
        item["parent_shot_ids"][0]
        for item in shot_diffs
        if item["relation"] == "deleted"
    }
    ordered_counts = {
        name: relation_counts[name]
        for name in (*LINEAGE_RELATIONS, "lineage_needs_review")
        if relation_counts[name]
    }
    return {
        "schema_version": VERSION_DIFF_SCHEMA_VERSION,
        "project_id": project,
        "parent_version_id": parent_version,
        "child_version_id": child_version,
        "parent_shots": [_public_shot(item) for item in parents],
        "child_shots": [_public_shot(item) for item in children],
        "shot_diffs": shot_diffs,
        "lineage_needs_review": bool(review_items),
        "lineage_review_items": review_items,
        "summary": {
            "parent_shot_count": len(parents),
            "child_shot_count": len(children),
            "relation_counts": ordered_counts,
            "changed_shot_count": len(changed_child_ids | deleted_parent_ids),
            "changed_field_count": changed_field_count,
            "field_change_counts": _field_change_counts(shot_diffs),
            "position_changed_count": sum(item["position_changed"] for item in shot_diffs),
            "lineage_needs_review_count": len(review_items),
        },
    }


def version_shots_from_artifacts(
    script_rows: list[dict],
    detail_rows: list[dict],
    *,
    bindings: list[dict] | None = None,
    shot_ids_by_no: dict[str, str] | None = None,
) -> list[dict]:
    """Adapt current ScriptDraft/detail/binding artifacts to diff input.

    This is intentionally a pure adapter. It does not read run directories or
    update version storage, so E01 can connect its repository contract later.
    """
    if len(script_rows) != len(detail_rows):
        raise ValueError("script_rows 与 detail_rows 数量不一致")
    binding_by_shot = {
        str(item.get("shot_no") or "").strip(): dict(item)
        for item in bindings or []
        if str(item.get("shot_no") or "").strip()
    }
    identities = {str(key): str(value) for key, value in (shot_ids_by_no or {}).items()}
    result = []
    for script, detail in zip(script_rows, detail_rows):
        shot_no = str(script.get("镜号") or script.get("shot_no") or "").strip()
        if not shot_no:
            raise ValueError("script_rows 存在缺少镜号的记录")
        fields = {
            key: deepcopy(value)
            for key, value in script.items()
            if key not in {"镜号", "shot_no", "shot_id"}
        }
        binding = binding_by_shot.get(shot_no, {})
        for key in ("image_id", "original_path", "decision", "resolution_status", "basis"):
            if key in binding:
                fields[key] = deepcopy(binding[key])
        refs = deepcopy(detail.get("source_refs") or [])
        if not refs:
            source_ref = {
                "source_file": detail.get("采用来源文件") or detail.get("source_file") or "",
                "source_sheet": detail.get("采用来源 sheet") or detail.get("source_sheet") or "",
                "source_row": detail.get("采用来源行") or detail.get("source_row") or "",
            }
            refs = [source_ref] if all(str(value).strip() for value in source_ref.values()) else []
        item = {
            "shot_no": shot_no,
            "fields": fields,
            "source_refs": refs,
        }
        shot_id = identities.get(shot_no) or str(script.get("shot_id") or "").strip()
        if shot_id:
            item["shot_id"] = shot_id
        result.append(item)
    return result


def build_revision_report(
    *,
    diff: dict,
    original_feedback: str,
    structured_intent: dict,
    expected_impact: dict,
    locked_fields: list[dict],
    rerun_nodes: list[str],
    unresolved_items: list,
    machine_review: dict,
    human_review: dict,
    publication: dict,
) -> dict:
    """Build a revision report whose completion is computed, not model-rated."""
    if diff.get("schema_version") != VERSION_DIFF_SCHEMA_VERSION:
        raise ValueError("diff 不是受支持的 mplus-version-diff/v1")
    locks = _verify_locked_fields(diff, locked_fields)
    actual = _actual_impact(diff, expected_impact)
    completion = _completion_status(
        diff=diff,
        locks=locks,
        unresolved_items=unresolved_items,
        machine_review=machine_review,
        human_review=human_review,
        publication=publication,
        actual_impact=actual,
    )
    return {
        "schema_version": REVISION_REPORT_SCHEMA_VERSION,
        "project_id": str(diff.get("project_id") or ""),
        "parent_version_id": str(diff.get("parent_version_id") or ""),
        "child_version_id": str(diff.get("child_version_id") or ""),
        "original_feedback": str(original_feedback or "").strip(),
        "structured_intent": deepcopy(structured_intent or {}),
        "expected_impact": deepcopy(expected_impact or {}),
        "actual_impact": actual,
        "locked_field_verification": locks,
        "rerun_nodes": [str(item) for item in rerun_nodes if str(item).strip()],
        "lineage": {
            "relation_counts": deepcopy(diff.get("summary", {}).get("relation_counts") or {}),
            "lineage_needs_review": bool(diff.get("lineage_needs_review")),
            "review_items": deepcopy(diff.get("lineage_review_items") or []),
        },
        "unresolved_items": deepcopy(unresolved_items or []),
        "statuses": {
            "machine_review": deepcopy(machine_review or {}),
            "human_review": deepcopy(human_review or {}),
            "publication": deepcopy(publication or {}),
        },
        "completion": completion,
    }


def _resolve_lineage(parents: list[dict], children: list[dict]):
    unmatched_parents = set(range(len(parents)))
    unmatched_children = set(range(len(children)))
    relationships = []
    review_by_child = {}

    parent_by_id = {item["shot_id"]: index for index, item in enumerate(parents)}
    for child_index in sorted(unmatched_children):
        child = children[child_index]
        if not child["provided_shot_id"]:
            continue
        parent_index = parent_by_id.get(child["shot_id"])
        if parent_index is None or parent_index not in unmatched_parents:
            continue
        relationships.append((_continuity_relation(parents[parent_index], child), parent_index, child_index))
        unmatched_parents.remove(parent_index)
        unmatched_children.remove(child_index)

    _match_unique_key(
        parents,
        children,
        unmatched_parents,
        unmatched_children,
        relationships,
        review_by_child,
        key=lambda item: item["identity_key"],
        evidence="identity_key_not_unique",
    )
    _match_unique_key(
        parents,
        children,
        unmatched_parents,
        unmatched_children,
        relationships,
        review_by_child,
        key=lambda item: item["source_tokens"],
        evidence="source_evidence_not_unique",
    )
    _match_unique_key(
        parents,
        children,
        unmatched_parents,
        unmatched_children,
        relationships,
        review_by_child,
        key=lambda item: (item["source_tokens"], item["content_key"]),
        evidence="content_evidence_not_unique",
    )

    ambiguous_parents = {
        parent_index
        for item in review_by_child.values()
        for parent_index in item["candidate_parent_indices"]
    }
    unmatched_parents -= ambiguous_parents
    unmatched_children -= set(review_by_child)

    proposals = _structural_proposals(
        parents,
        children,
        unmatched_parents,
        unmatched_children,
    )
    usage = Counter(index for proposal in proposals for index in proposal["parent_indices"])
    child_usage = Counter(index for proposal in proposals for index in proposal["child_indices"])
    accepted = [
        proposal
        for proposal in proposals
        if all(usage[index] == 1 for index in proposal["parent_indices"])
        and all(child_usage[index] == 1 for index in proposal["child_indices"])
    ]
    conflicted = [proposal for proposal in proposals if proposal not in accepted]
    for proposal in conflicted:
        for child_index in proposal["child_indices"]:
            review_by_child.setdefault(child_index, {
                "reason": "structural_lineage_not_unique",
                "candidate_parent_indices": set(),
            })["candidate_parent_indices"].update(proposal["parent_indices"])
        unmatched_parents -= set(proposal["parent_indices"])
        unmatched_children -= set(proposal["child_indices"])
    for proposal in accepted:
        relation = proposal["relation"]
        for parent_index in proposal["parent_indices"]:
            for child_index in proposal["child_indices"]:
                if relation == "split_from" and len(proposal["parent_indices"]) != 1:
                    continue
                if relation == "merged_from" and len(proposal["child_indices"]) != 1:
                    continue
                relationships.append((relation, parent_index, child_index))
        unmatched_parents -= set(proposal["parent_indices"])
        unmatched_children -= set(proposal["child_indices"])

    relationships.extend(("new", None, index) for index in sorted(unmatched_children))
    relationships.extend(("deleted", index, None) for index in sorted(unmatched_parents))
    relationships.sort(key=lambda item: (
        len(children) if item[2] is None else item[2],
        len(parents) if item[1] is None else item[1],
        item[0],
    ))
    review_items = []
    for child_index in sorted(review_by_child):
        item = review_by_child[child_index]
        candidate_indices = sorted(item["candidate_parent_indices"])
        review_items.append({
            "child_shot_id": children[child_index]["shot_id"],
            "child_shot_no": children[child_index]["shot_no"],
            "candidate_parent_shot_ids": [parents[index]["shot_id"] for index in candidate_indices],
            "candidate_parent_shot_nos": [parents[index]["shot_no"] for index in candidate_indices],
            "reason": item["reason"],
        })
    return relationships, review_items


def _match_unique_key(
    parents,
    children,
    unmatched_parents,
    unmatched_children,
    relationships,
    review_by_child,
    *,
    key,
    evidence,
):
    parent_groups = defaultdict(list)
    child_groups = defaultdict(list)
    for index in sorted(unmatched_parents):
        value = key(parents[index])
        if value:
            parent_groups[value].append(index)
    for index in sorted(unmatched_children):
        if children[index]["provided_shot_id"]:
            continue
        value = key(children[index])
        if value:
            child_groups[value].append(index)
    for value in sorted(set(parent_groups) & set(child_groups), key=repr):
        parent_indices = parent_groups[value]
        child_indices = child_groups[value]
        if len(parent_indices) == len(child_indices) == 1:
            parent_index = parent_indices[0]
            child_index = child_indices[0]
            relationships.append((
                _continuity_relation(parents[parent_index], children[child_index]),
                parent_index,
                child_index,
            ))
            unmatched_parents.remove(parent_index)
            unmatched_children.remove(child_index)
            review_by_child.pop(child_index, None)
            continue
        for child_index in child_indices:
            review_by_child[child_index] = {
                "reason": evidence,
                "candidate_parent_indices": set(parent_indices),
            }


def _structural_proposals(parents, children, parent_indices, child_indices):
    proposals = []
    for parent_index in sorted(parent_indices):
        target = set(parents[parent_index]["source_tokens"])
        if len(target) < 2:
            continue
        partitions = _exact_partitions(
            target,
            {
                index: set(children[index]["source_tokens"])
                for index in child_indices
            },
        )
        for partition in partitions:
            proposals.append({
                "relation": "split_from",
                "parent_indices": (parent_index,),
                "child_indices": tuple(partition),
            })
    for child_index in sorted(child_indices):
        target = set(children[child_index]["source_tokens"])
        if len(target) < 2:
            continue
        partitions = _exact_partitions(
            target,
            {
                index: set(parents[index]["source_tokens"])
                for index in parent_indices
            },
        )
        for partition in partitions:
            proposals.append({
                "relation": "merged_from",
                "parent_indices": tuple(partition),
                "child_indices": (child_index,),
            })
    return proposals


def _exact_partitions(target: set, candidates: dict[int, set]) -> list[tuple[int, ...]]:
    eligible = {
        index: tokens
        for index, tokens in candidates.items()
        if tokens and tokens < target
    }
    by_token = defaultdict(list)
    for index, tokens in eligible.items():
        for token in tokens:
            by_token[token].append(index)
    solutions = set()

    def visit(remaining, chosen):
        if not remaining:
            if len(chosen) >= 2:
                solutions.add(tuple(sorted(chosen)))
            return
        if len(solutions) > 100:
            return
        token = min(remaining, key=repr)
        for index in sorted(by_token.get(token, [])):
            tokens = eligible[index]
            if not tokens <= remaining:
                continue
            visit(remaining - tokens, (*chosen, index))

    visit(set(target), ())
    return sorted(solutions)


def _shot_diff(relation, parents, children, parent_index, child_index):
    parent = parents[parent_index] if parent_index is not None else None
    child = children[child_index] if child_index is not None else None
    before_fields = dict(parent["fields"]) if parent else {}
    after_fields = dict(child["fields"]) if child else {}
    before_fields["source_refs"] = parent["source_refs"] if parent else None
    after_fields["source_refs"] = child["source_refs"] if child else None
    fields = []
    for name in sorted(set(before_fields) | set(after_fields)):
        before = deepcopy(before_fields.get(name))
        after = deepcopy(after_fields.get(name))
        if parent is None:
            status = "added"
        elif child is None:
            status = "deleted"
        else:
            status = "unchanged" if before == after else "changed"
        fields.append({"field": name, "status": status, "before": before, "after": after})
    return {
        "relation": relation,
        "parent_shot_ids": [parent["shot_id"]] if parent else [],
        "parent_shot_nos": [parent["shot_no"]] if parent else [],
        "child_shot_id": child["shot_id"] if child else "",
        "child_shot_no": child["shot_no"] if child else "",
        "position_changed": bool(parent and child and parent["shot_no"] != child["shot_no"]),
        "field_diffs": fields,
    }


def _verify_locked_fields(diff: dict, locked_fields: list[dict]) -> dict:
    checks = []
    for lock in locked_fields or []:
        field = str(lock.get("field") or "").strip()
        if not field:
            raise ValueError("locked_fields 的 field 不得为空")
        canonical_field = "source_refs" if field.casefold() in _SOURCE_FIELD_ALIASES else field
        matching = [
            item for item in diff.get("shot_diffs") or []
            if _lock_matches_shot(lock, item)
        ]
        ambiguous = [
            item for item in diff.get("lineage_review_items") or []
            if _lock_matches_review(lock, item)
        ]
        changes = [
            field_diff
            for shot in matching
            for field_diff in shot.get("field_diffs") or []
            if field_diff.get("field") == canonical_field
            and field_diff.get("status") != "unchanged"
        ]
        expected_marker = lock.get("expected_value", _Missing)
        expected_mismatches = []
        if expected_marker is not _Missing:
            expected_mismatches = [
                field_diff
                for shot in matching
                for field_diff in shot.get("field_diffs") or []
                if field_diff.get("field") == canonical_field
                and field_diff.get("after") != expected_marker
            ]
        status = (
            "lineage_needs_review" if ambiguous
            else "violated" if changes or expected_mismatches
            else "not_found" if not matching
            else "preserved"
        )
        checks.append({
            "shot_id": str(lock.get("shot_id") or ""),
            "shot_no": str(lock.get("shot_no") or ""),
            "field": field,
            "status": status,
            "evidence": deepcopy(changes or expected_mismatches),
        })
    violations = [item for item in checks if item["status"] in {"violated", "not_found"}]
    needs_review = [item for item in checks if item["status"] == "lineage_needs_review"]
    return {
        "checks": checks,
        "violations": violations,
        "lineage_needs_review": needs_review,
        "all_preserved": not violations and not needs_review,
    }


def _actual_impact(diff: dict, expected: dict) -> dict:
    changed_fields = sorted({
        field["field"]
        for shot in diff.get("shot_diffs") or []
        for field in shot.get("field_diffs") or []
        if field.get("status") != "unchanged"
    })
    changed_shot_ids = []
    changed_shot_nos = []
    for shot in diff.get("shot_diffs") or []:
        changed = (
            shot.get("relation") not in {"preserved", "moved"}
            or shot.get("position_changed")
            or any(field.get("status") != "unchanged" for field in shot.get("field_diffs") or [])
        )
        if not changed:
            continue
        if shot.get("child_shot_id"):
            changed_shot_ids.append(str(shot["child_shot_id"]))
        if shot.get("child_shot_no"):
            changed_shot_nos.append(str(shot["child_shot_no"]))
        elif shot.get("parent_shot_nos"):
            changed_shot_nos.extend(str(item) for item in shot["parent_shot_nos"])
    expected_fields = {str(item) for item in expected.get("fields") or []}
    expected_shots = {str(item) for item in expected.get("shot_nos") or []}
    actual_fields = set(changed_fields)
    actual_shots = set(changed_shot_nos)
    allowed_fields = {
        str(item) for item in expected.get("allowed_unexpected_fields") or []
    }
    allowed_shots = {
        str(item) for item in expected.get("allowed_unexpected_shot_nos") or []
    }
    return {
        "changed_shot_ids": _unique(changed_shot_ids),
        "changed_shot_nos": _unique(changed_shot_nos),
        "changed_fields": changed_fields,
        "relation_counts": deepcopy(diff.get("summary", {}).get("relation_counts") or {}),
        "unexpected_fields": sorted(actual_fields - expected_fields - allowed_fields),
        "missing_expected_fields": sorted(expected_fields - actual_fields),
        "unexpected_shot_nos": sorted(actual_shots - expected_shots - allowed_shots),
        "missing_expected_shot_nos": sorted(expected_shots - actual_shots),
        "lineage_needs_review": bool(diff.get("lineage_needs_review")),
    }


def _completion_status(
    *,
    diff,
    locks,
    unresolved_items,
    machine_review,
    human_review,
    publication,
    actual_impact=None,
):
    machine_status = str(machine_review.get("status") or "").strip().casefold()
    human_status = str(human_review.get("status") or "").strip().casefold()
    publication_status = str(publication.get("status") or "").strip().casefold()
    if diff.get("lineage_needs_review") or locks["lineage_needs_review"]:
        return {"status": "lineage_needs_review", "completed": False}
    if locks["violations"]:
        return {"status": "blocked", "completed": False, "reason": "locked_field_violation"}
    if machine_status in {"fail", "failed", "blocked", "hard_fail"}:
        return {"status": "blocked", "completed": False, "reason": "machine_review_blocked"}
    if machine_status in {"", "pending", "pending_review", "waiting", "waiting_machine_review"}:
        return {"status": "waiting_machine_review", "completed": False}
    if machine_status not in {"pass", "passed", "quality_pass"}:
        return {"status": "blocked", "completed": False, "reason": "machine_review_status_unknown"}
    if human_status in {"quality_fail", "failed", "rejected"}:
        return {"status": "blocked", "completed": False, "reason": "human_quality_fail"}
    if publication_status == "published" and human_status != "quality_pass":
        return {"status": "blocked", "completed": False, "reason": "publication_gate_violation"}
    actual = actual_impact or {}
    missing = {
        "fields": list(actual.get("missing_expected_fields") or []),
        "shot_nos": list(actual.get("missing_expected_shot_nos") or []),
    }
    unexpected = {
        "fields": list(actual.get("unexpected_fields") or []),
        "shot_nos": list(actual.get("unexpected_shot_nos") or []),
    }
    has_missing = any(missing.values())
    has_unexpected = any(unexpected.values())
    if has_missing or has_unexpected:
        reason = (
            "impact_mismatch"
            if has_missing and has_unexpected
            else "expected_impact_missing"
            if has_missing
            else "unexpected_impact"
        )
        return {
            "status": "partially_completed",
            "completed": False,
            "reason": reason,
            "missing": missing,
            "unexpected": unexpected,
        }
    if unresolved_items:
        return {"status": "partially_completed", "completed": False}
    if human_status != "quality_pass":
        return {"status": "waiting_human_review", "completed": False}
    return {"status": "completed", "completed": True}


def _normalize_shots(project_id: str, version_id: str, shots: list[dict]) -> list[dict]:
    normalized = []
    seen_ids = set()
    seen_numbers = set()
    for index, raw in enumerate(shots or []):
        if not isinstance(raw, dict):
            raise ValueError("shot 必须是对象")
        shot_no = str(raw.get("shot_no") or raw.get("镜号") or "").strip()
        if not shot_no:
            raise ValueError("shot_no 不得为空")
        if shot_no in seen_numbers:
            raise ValueError(f"同一版本存在重复 shot_no: {shot_no}")
        seen_numbers.add(shot_no)
        fields = dict(raw.get("fields") or {
            key: value for key, value in raw.items()
            if key not in _SHOT_METADATA_FIELDS and key != "镜号"
        })
        source_refs = _normalize_sources(raw.get("source_refs") or [])
        provided_id = str(raw.get("shot_id") or "").strip()
        shot_id = provided_id or _stable_shot_id(
            project_id,
            version_id,
            index,
            shot_no,
            source_refs,
            fields,
        )
        if shot_id in seen_ids:
            raise ValueError(f"同一版本存在重复 shot_id: {shot_id}")
        seen_ids.add(shot_id)
        normalized.append({
            "shot_id": shot_id,
            "provided_shot_id": bool(provided_id),
            "shot_no": shot_no,
            "fields": fields,
            "source_refs": source_refs,
            "source_tokens": tuple(_canonical_json(item) for item in source_refs),
            "identity_key": str(raw.get("identity_key") or "").strip(),
            "content_key": _canonical_json(fields),
        })
    return normalized


def _normalize_sources(refs: Iterable[dict]) -> list[dict]:
    normalized = []
    for raw in refs:
        if not isinstance(raw, dict):
            raise ValueError("source_refs 每项必须是对象")
        item = {
            "source_file": str(raw.get("source_file") or raw.get("来源文件") or "").strip(),
            "source_sheet": str(raw.get("source_sheet") or raw.get("来源sheet") or raw.get("来源 sheet") or "").strip(),
            "source_row": raw.get("source_row") or raw.get("来源行") or "",
        }
        if not all(str(item[key]).strip() for key in item):
            raise ValueError("source_refs 必须包含来源文件、来源 Sheet、来源行")
        normalized.append(item)
    deduplicated = {_canonical_json(item): item for item in normalized}
    return [deduplicated[key] for key in sorted(deduplicated)]


def _stable_shot_id(project_id, version_id, index, shot_no, source_refs, fields):
    digest = hashlib.sha256(_canonical_json({
        "project_id": project_id,
        "origin_version_id": version_id,
        "ordinal": index,
        "shot_no": shot_no,
        "source_refs": source_refs,
        "fields": fields,
    }).encode("utf-8")).hexdigest()
    return f"shot_{digest[:20]}"


def _continuity_relation(parent, child):
    return "preserved" if parent["shot_no"] == child["shot_no"] else "moved"


def _public_shot(item):
    return {
        "shot_id": item["shot_id"],
        "shot_no": item["shot_no"],
        "fields": deepcopy(item["fields"]),
        "source_refs": deepcopy(item["source_refs"]),
    }


def _field_change_counts(shot_diffs):
    counts = Counter(
        field["field"]
        for shot in shot_diffs
        for field in shot["field_diffs"]
        if field["status"] != "unchanged"
    )
    return {name: counts[name] for name in sorted(counts)}


def _lock_matches_shot(lock, shot):
    shot_id = str(lock.get("shot_id") or "").strip()
    shot_no = str(lock.get("shot_no") or "").strip()
    if shot_id and shot_id not in ({shot.get("child_shot_id")} | set(shot.get("parent_shot_ids") or [])):
        return False
    if shot_no and shot_no not in ({shot.get("child_shot_no")} | set(shot.get("parent_shot_nos") or [])):
        return False
    return True


def _lock_matches_review(lock, item):
    shot_id = str(lock.get("shot_id") or "").strip()
    shot_no = str(lock.get("shot_no") or "").strip()
    if shot_id and shot_id != item.get("child_shot_id"):
        return False
    if shot_no and shot_no != item.get("child_shot_no"):
        return False
    return True


def _required_id(value, name):
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} 不得为空")
    return normalized


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique(values):
    return list(dict.fromkeys(values))


class _MissingType:
    pass


_Missing = _MissingType()
