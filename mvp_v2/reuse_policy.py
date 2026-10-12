from __future__ import annotations

import re


_ALLOWED_LEVELS = {
    "可复用",
    "镜头功能可复用",
    "需适配",
    "需改编",
    "限制使用",
}
_BLOCKED_TERMS = ("禁用", "禁止", "不可复用", "不得复用", "停用")


def normalize_reuse_level(value: str) -> str:
    normalized = re.sub(r"\s+", "", str(value or ""))
    if not normalized:
        return ""
    if any(term in normalized for term in _BLOCKED_TERMS):
        return "禁用"
    if normalized in _ALLOWED_LEVELS:
        return normalized
    return "禁用"


def is_reuse_blocked(value: str) -> bool:
    return normalize_reuse_level(value) == "禁用"
