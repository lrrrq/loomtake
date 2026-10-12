from __future__ import annotations

import argparse

from .expansion_decision import (
    ExpansionDecisionError,
    build_expansion_evidence,
    decide_type_expansion,
    write_expansion_decision,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="根据三个发布会项目证据决定是否进入展会类型")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--freeze-manifest", required=True)
    parser.add_argument("--freeze-manifest-sha256", required=True)
    parser.add_argument("--blind-input-seal", required=True)
    parser.add_argument("--blind-input-seal-sha256", required=True)
    parser.add_argument("--timing-evidence", required=True)
    parser.add_argument("--timing-evidence-sha256", required=True)
    parser.add_argument(
        "--blind-case",
        nargs=2,
        metavar=("PROJECT_CACHE", "REVIEW_WORKBOOK"),
        required=True,
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        evidence = build_expansion_evidence(
            args.project_root,
            args.freeze_manifest,
            args.freeze_manifest_sha256,
            args.blind_input_seal,
            args.blind_input_seal_sha256,
            tuple(args.blind_case),
            args.timing_evidence,
            args.timing_evidence_sha256,
        )
        projects = evidence["projects"]
        common_rework = evidence["common_rework_count"]
        common_all_three = evidence["common_all_three_count"]
        decision = decide_type_expansion(projects, common_rework, common_all_three)
        output = write_expansion_decision(args.output, decision)
    except ExpansionDecisionError as exc:
        print(f"类型扩展决策失败: {exc}")
        return 1
    print(f"类型扩展决策: decision={decision['decision']}, output={output}")
    if decision["decision"] == "enter_exhibition":
        return 0
    if decision["decision"] == "continue_launch":
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
