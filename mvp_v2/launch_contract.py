from __future__ import annotations


RECOMMENDED_MIN_SLOTS = 12
RECOMMENDED_MAX_SLOTS = 18


def template_count_valid(count: int) -> bool:
    return int(count or 0) > 0


def template_count_recommended(count: int) -> bool:
    return RECOMMENDED_MIN_SLOTS <= int(count or 0) <= RECOMMENDED_MAX_SLOTS


def expected_slot_numbers(count: int) -> tuple[int, ...]:
    return tuple(range(1, max(0, int(count or 0)) + 1))


def expected_adjacent_edges(count: int) -> tuple[str, ...]:
    return tuple(f"{index}->{index + 1}" for index in range(1, max(0, int(count or 0))))


def contract_counts_valid(shot_count: int, adjacent_edge_count: int, full_film_count: int) -> bool:
    count = int(shot_count or 0)
    return count > 0 and int(adjacent_edge_count or 0) == count - 1 and int(full_film_count or 0) == 1
