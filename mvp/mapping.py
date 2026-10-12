from __future__ import annotations

from .models import SourceShot

SCENE_TYPES = ("发布会", "音乐节", "年会", "展会", "赛事")


class MappingError(ValueError):
    pass


def text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def detect_scene_type(brief: dict) -> str:
    filename = text(brief.get("项目区域文件名"))
    for scene_type in SCENE_TYPES:
        if scene_type in filename:
            return scene_type

    declared = text(brief.get("脚本类型"))
    for scene_type in SCENE_TYPES:
        if scene_type in declared:
            return scene_type

    raise MappingError("无法判断脚本类型")


def select_candidates(brief: dict, shots: list[SourceShot], limit: int = 40) -> list[SourceShot]:
    scene_type = detect_scene_type(brief)
    same_type = [shot for shot in shots if shot.scene_type == scene_type]
    structure_hint = text(brief.get("结构"))

    if structure_hint:
        exact = [shot for shot in same_type if shot.structure and shot.structure in structure_hint]
        rest = [shot for shot in same_type if shot not in exact]
        ordered = exact + rest
    else:
        ordered = same_type

    return ordered[:limit]

