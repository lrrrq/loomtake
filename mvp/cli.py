from __future__ import annotations

import argparse
import os
from pathlib import Path

from .excel_io import load_source_shots, read_briefs, write_output_workbook
from .generator import CodexGenerator
from .mapping import select_candidates
from mvp_v2.runtime_config import load_runtime_config

DEFAULT_DEBUG_DIR = Path("/tmp/mplus-script-mvp-debug")


def _default_source_root() -> Path:
    return Path(load_runtime_config()["source_root"])


def _default_codex_bin() -> str:
    return str(os.environ.get("MPLUS_CODEX_BIN") or "codex").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Excel 驱动脚本生产 MVP")
    parser.add_argument("--input", required=True, help="项目需求 Excel")
    parser.add_argument("--output", required=True, help="输出脚本 Excel")
    parser.add_argument("--source-root", default=str(_default_source_root()), help="脚本资料目录")
    parser.add_argument("--codex-bin", default=_default_codex_bin(), help="Codex CLI 路径")
    parser.add_argument("--debug-dir", default=str(DEFAULT_DEBUG_DIR), help="Codex prompt/response 调试目录")
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parents[1]
    schema_path = project_root / "schemas" / "codex_shots.schema.json"
    briefs = read_briefs(args.input)
    source_shots = load_source_shots(args.source_root)
    generator = CodexGenerator(args.codex_bin, project_root, schema_path, args.debug_dir)

    generated = []
    for brief in briefs:
        candidates = select_candidates(brief, source_shots, limit=40)
        result = generator.generate(brief, candidates)
        generated.append({"brief": brief, "shots": result["shots"]})

    write_output_workbook(args.output, generated, source_root=args.source_root)
    print(f"写出: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
