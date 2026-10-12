from __future__ import annotations


SOURCE_REUSE_POLICIES = {
    ("M+发布会类通用脚本库.xlsx", "模版"): {
        "reuse_level": "可复用",
        "forbidden_note": "发布会基础剪辑结构；替换后仍须通过槽位职责、视觉证据和品牌审计",
    },
    ("M+发布会类通用脚本库.xlsx", "创意库"): {
        "reuse_level": "镜头功能可复用",
        "forbidden_note": "不得直接复用原品牌文案；视觉素材须通过品牌和视觉证据审计",
    },
}


def source_reuse_policy(source_file: str, source_sheet: str) -> dict[str, str] | None:
    filename = str(source_file)
    if filename.startswith("M+发布会类通用脚本库") and filename.endswith(".xlsx"):
        filename = "M+发布会类通用脚本库.xlsx"
    policy = SOURCE_REUSE_POLICIES.get((filename, str(source_sheet)))
    return dict(policy) if policy else None
