from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from .models import SourceShot


class GenerationError(RuntimeError):
    pass


def build_prompt(brief: dict, candidates: list[SourceShot]) -> str:
    target = _target_from_brief(brief)
    payload = {
        "task": "请根据项目 brief 和候选参考镜头，生成镜头级脚本。只输出符合 schema 的 JSON，不要输出解释。",
        "brief": brief,
        "requirements": {
            **target,
            "fields": [
                "structure",
                "duration_seconds",
                "effect",
                "shot_size",
                "camera_movement",
                "description",
                "audio",
                "note",
                "source_file",
                "source_sheet",
                "source_row",
                "reference_image_path",
                "effect_reference_path",
                "match_reason",
            ],
            "style": "画面具体，可制作，保留来源，不写泛文案。",
            "source_rules": [
                "每条镜头必须选择一个 candidate_shots 作为主要来源。",
                "source_file、source_sheet、source_row 必须原样复制所选候选镜头。",
                "如果所选候选镜头有 reference_image_path 或 effect_reference_path，必须原样复制到输出。",
                "没有图片锚点时才输出空字符串。"
            ],
        },
        "candidate_shots": [shot.prompt_dict() for shot in candidates],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _target_from_brief(brief: dict) -> dict:
    duration = str(brief.get("视频时长", ""))
    match = re.search(r"\d+", duration)
    if not match:
        return {}
    seconds = int(match.group(0))
    target_count = 16 if 50 <= seconds <= 70 else max(1, round(seconds / 4))
    return {
        "target_shot_count": target_count,
        "target_total_duration_seconds": seconds,
        "structure": "输出 shots 数量必须等于 target_shot_count，所有 duration_seconds 相加必须等于 target_total_duration_seconds。",
    }


class CodexGenerator:
    def __init__(self, codex_bin: str, project_root: str | Path, schema_path: str | Path, debug_dir: str | Path):
        self.codex_bin = str(codex_bin)
        self.project_root = Path(project_root)
        self.schema_path = Path(schema_path)
        self.debug_dir = Path(debug_dir)

    def generate(self, brief: dict, candidates: list[SourceShot]) -> dict:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        row_id = brief.get("input_row", "unknown")
        prompt = build_prompt(brief, candidates)
        prompt_path = self.debug_dir / f"row_{row_id}_prompt.txt"
        response_path = self.debug_dir / f"row_{row_id}_response.txt"
        prompt_path.write_text(prompt, encoding="utf-8")

        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp:
            tmp_response = Path(tmp.name)

        cmd = [
            self.codex_bin,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(self.schema_path),
            "--output-last-message",
            str(tmp_response),
            "-C",
            str(self.project_root),
            "-",
        ]
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=self.project_root,
        )
        raw = tmp_response.read_text(encoding="utf-8") if tmp_response.exists() else ""
        response_path.write_text(raw, encoding="utf-8")
        tmp_response.unlink(missing_ok=True)

        if proc.returncode != 0:
            raise GenerationError(f"Codex 调用失败: {proc.stderr.strip() or proc.stdout.strip()}")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GenerationError(f"Codex 返回非 JSON: {exc}") from exc

        if not isinstance(data, dict) or not isinstance(data.get("shots"), list):
            raise GenerationError("Codex JSON 缺少 shots 数组")
        return data
