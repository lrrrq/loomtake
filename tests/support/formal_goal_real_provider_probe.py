"""Opt-in, bounded real editor acceptance on an anonymous ten-shot script.

This exercises the production assistant leaf and the configured HTTP provider.
The after.json file is an operation projection for content review, not a claim
that a draft, version, image decision, review, or delivery has been written.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from openpyxl import Workbook

from mvp_v2 import text_providers
from mvp_v2.formal_runtime import FormalCapabilityRouter, DEEPSEEK_EDITOR_MAX_OUTPUT_TOKENS
from mvp_v2.formal_script_assistant import (
    FormalScriptAssistantEditorPolicy, analyze_formal_script_instruction,
)
from mvp_v2.runtime_config import load_local_secrets
from mvp_v2.text_providers import build_text_provider


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def anonymous_fixture():
    rows = [
        ("开场空间", "全景", "固定全景交代室内活动厅入口、演示桌和观众席的位置关系，三个区域在同一画面中可辨。", "Brief 必拍远景，必须保留空间位置关系，不能改用局部细节替代。"),
        ("到场引导", "中景", "演示人员在入口侧引导到场观众走向演示桌，固定中景保留两者的交流关系。", "建立由入口到演示区的动线。"),
        ("操作展示", "中景", "演示人员伸出手指按下白色演示样机面板上唯一的圆形按钮，固定中景同时看见手和样机。", "圆形按钮与手指的接触细节可作为产品展示重点。"),
        ("演示关系", "全景", "演示人员、白色样机与桌旁观察的观众同框，固定全景展示操作方与观察方的位置关系。", "保留必要的人物和操作对象关系，避免整段只剩孤立细节。"),
        ("人物表达", "中景", "主讲人手持麦克风说明操作步骤，固定中景记录其面部与持麦动作，不引用未提供的具体发言。", "人物表达可从面部与持麦动作中选择一个明确重心。"),
        ("已有细节", "特写", "固定特写呈现白色面板上的圆形按钮与演示人员的指尖接触，不展示任何未经确认的功能效果。", "已有特写，无需为增加特写重复修改。"),
        ("观众体验", "中景", "观众在演示人员指引下尝试按动圆形按钮，固定中景保留操作动作与指导关系。", "体验关系不同于演示人员独自操作，不编造成效或满意反应。"),
        ("交接关系", "中景", "主讲人与演示人员同框交接麦克风，固定中景完整保留双方的姿态和交接关系。", "用户已保护本镜，所有字段保持原样。"),
        ("人物聆听", "中景", "观众面向主讲人聆听，固定中景记录自然姿态，手部放松，不预设笑容、掌声或欢呼。", "可用一位观众的中性表情承接主讲人表达，但不虚构身份与情绪。"),
        ("空间收束", "全景", "固定全景回到演示桌与观众所在的活动空间，保留人与样机的关系作为收束。", "与开场形成空间呼应，不新增活动结果。"),
    ]
    shots = [{
        "shot_id": f"anonymous-shot-{number}", "shot_no": str(number),
        "structure": structure, "camera_type": "实拍", "shot_size": size,
        "camera_move": "固定", "duration_seconds": 6, "visual": visual,
        "sound": "匿名曲目A重新起乐，现场氛围与音乐推进。",
        "voiceover": "", "subtitle": "", "notes": notes,
        "source": {"file": "anonymous-ten-shots.xlsx", "sheet": "脚本", "row": number + 1},
    } for number, (structure, size, visual, notes) in enumerate(rows, 1)]
    shots[7]["sound"] = "匿名曲目A持续铺底，交接麦克风时收低环境声。"
    context = {
        "brief": {
            "项目名称": "匿名产品体验活动", "品牌或客户": "匿名品牌", "视频时长": "60秒",
            "风格调性": "克制、真实、清楚说明操作与人物关系",
            "已确认场景与对象": "室内活动厅、入口、演示桌、观众席；白色演示样机面板只有一个圆形按钮；演示人员、主讲人和到场观众；手持麦克风。",
            "已确认环节": "到场引导、按钮操作演示、主讲人说明步骤、观众尝试、麦克风交接、空间收束。现稿是待拍安排，不宣称活动已发生。",
            "必须出现的内容": "第1镜必须保持全景或远景，且在同一画面中交代入口、演示桌、观众席的位置关系；保留操作展示、人物表达、体验指导与收束。",
            "禁止或不能出现的内容": "不新增具体人物身份、品牌标志、产品功能、灯光或屏幕反馈、人数、销量、活动结果；不虚构掌声、欢呼、笑容、满意等现场反应。",
            "特殊需求": "全片使用已确认的匿名曲目A作为同一条连续BGM，不能逐镜重新起乐、替换曲目或改成另一条配乐；必要现场声须低于可辨的人声；未录到的声音必须标明声音建议。",
        },
        "creative_standard": {
            "叙事顺序": "空间建立→到场→操作展示→关系→主讲表达→已有细节→体验→交接→聆听→空间收束",
            "事实优先": "保留已确认事实与必拍覆盖；模板句式允许改写；原创构图仅是拍摄建议。",
            "拍摄约束": "所有镜头实拍、固定机位，不新增器材、运动、场景、物料或未确认的产品效果。",
        },
        "current_script_summary": "10镜，共60秒。现有1个特写。开场空间远景是必拍要求；第8镜已受保护。声音重复且错误地逐镜重新起乐。",
        "neighbor_shots": deepcopy(shots),
        "source_summary": {
            "kind": "anonymous_test_workbook", "references": deepcopy(shots),
            "reference_role": "匿名测试原稿的真实来源行；不能据此声称已有拍摄素材或已验证新构图。",
        },
        "technical_candidates": {"camera_type": ["实拍"], "shot_size": ["全景", "中景", "特写"], "camera_move": ["固定"]},
        "allow_original_adaptation": True,
    }
    return shots, context


CASES = [
    {"name": "goal", "instruction": "增加特写", "selected": None},
    {"name": "selected-all", "instruction": "所选镜头全部改为特写", "selected": [3, 5, 6]},
    {"name": "count-two", "instruction": "把其中2镜改成特写", "selected": None},
    {"name": "sound-natural", "instruction": "声音做些递进，听起来层次丰富些", "selected": None},
    {"name": "single", "instruction": "当前镜头增加特写", "selected": [3]},
    {"name": "subset-goal", "instruction": "增加特写", "selected": [3, 5, 9]},
    {"name": "mandatory-wide-only", "instruction": "增加特写", "selected": [1]},
    {"name": "already-closeup", "instruction": "增加特写", "selected": [6]},
    {"name": "protected-only", "instruction": "增加特写", "selected": [8]},
    {"name": "insufficient-count", "instruction": "增加2个特写", "selected": [3, 6]},
    {"name": "new-shots", "instruction": "新增2个镜头", "selected": None},
    {"name": "scope-conflict", "instruction": "第1镜增加特写", "selected": [3]},
]


def run_case(case, *, out, route, provider, shots, context):
    directory = out / case["name"]
    directory.mkdir()
    original = deepcopy(shots)
    selected = None if case["selected"] is None else [f"anonymous-shot-{number}" for number in case["selected"]]
    locked = ["anonymous-shot-8"]
    dump(directory / "before.json", original)
    dump(directory / "input.json", {"instruction": case["instruction"], "selected_shot_ids": selected,
                                    "assistant_locked_shot_ids": locked, "context": context})
    calls, transmissions = [], []
    transport = text_providers.urlopen

    class ObservedResponse:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            self.response.__enter__()
            return self

        def __exit__(self, *args):
            return self.response.__exit__(*args)

        def read(self):
            raw = self.response.read()
            (directory / "http-response.json").write_bytes(raw)
            payload = json.loads(raw)
            transmissions[-1].update(model=payload.get("model"), usage=payload.get("usage"),
                finish_reasons=[choice.get("finish_reason") for choice in payload.get("choices", [])])
            return raw

    def observed_urlopen(*args, **kwargs):
        if transmissions:
            raise RuntimeError("One HTTP attempt per sample; automatic retries are disabled.")
        transmissions.append({"attempt": 1})
        response = transport(*args, **kwargs)
        transmissions[-1]["http_status"] = getattr(response, "status", None)
        return ObservedResponse(response)

    def invoke(prompt, model, max_tokens):
        if calls:
            raise RuntimeError("One editor invocation per sample.")
        budget = min(max_tokens, DEEPSEEK_EDITOR_MAX_OUTPUT_TOKENS)
        entry = {"model": model, "requested_max_tokens": max_tokens, "max_tokens": budget}
        calls.append(entry)
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        dump(directory / "prompt-data.json", json.loads(prompt.split("\nDATA:", 1)[1]))
        started = time.monotonic()
        try:
            with patch.object(text_providers, "urlopen", observed_urlopen), patch.object(text_providers, "PROVIDER_RETRY_DELAYS_SECONDS", ()):
                raw = provider(prompt, model, budget)
            (directory / "editor-raw.txt").write_text(raw, encoding="utf-8")
            entry["response_chars"] = len(raw)
            return raw
        except Exception as exc:
            entry.update(error_type=type(exc).__name__, error_code=getattr(exc, "code", None))
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - started, 3)

    result = analyze_formal_script_instruction(
        instruction=case["instruction"], shots=shots, selected_shot_ids=selected,
        assistant_locked_shot_ids=locked, context=context,
        editor_policy=FormalScriptAssistantEditorPolicy(invoke, route.provider, route.model),
    )
    dump(directory / "result.json", result)
    after = deepcopy(original)
    by_id = {shot["shot_id"]: shot for shot in after}
    operations = result.get("operations", [])
    for operation in operations:
        by_id[operation["shot_id"]][operation["field"]] = operation["value"]
    dump(directory / "after.json", after)
    changes = [{"shot_no": old["shot_no"], "field": field, "before": old.get(field), "after": new.get(field)}
               for old, new in zip(original, after) for field in old if old.get(field) != new.get(field)]
    dump(directory / "changes.json", changes)
    allowed_fields = {"sound"} if case["name"] == "sound-natural" else {"shot_size", "visual"}
    checks = {
        "input_snapshot_unchanged": shots == original,
        "total_shot_count_unchanged": len(after) == 10,
        "source_triples_unchanged": all(old["source"] == new["source"] for old, new in zip(original, after)),
        "camera_type_and_move_unchanged": all((old["camera_type"], old["camera_move"]) == (new["camera_type"], new["camera_move"]) for old, new in zip(original, after)),
        "protected_shot_unchanged": after[7] == original[7],
        "only_permitted_fields_changed": all(change["field"] in allowed_fields for change in changes),
        "explicit_selection_respected": selected is None or all(operation["shot_id"] in selected for operation in operations),
        "mandatory_opening_visual_and_size_preserved": all(after[0][field] == original[0][field] for field in ("shot_size", "visual")),
        "one_editor_call_at_most": len(calls) <= 1,
        "one_transport_attempt_at_most": len(transmissions) <= 1,
    }
    summary = {"case": case["name"], "instruction": case["instruction"], "selected_shots": case["selected"],
               "result_type": result.get("type"), "analysis_code": result.get("analysis_code"),
               "selection_mode": result.get("selection_mode"), "actual_changed_shot_ids": result.get("actual_changed_shot_ids", []),
               "changed_shot_numbers": sorted({item["shot_no"] for item in changes}, key=int),
               "shot_reasons": result.get("shot_reasons", []), "checks": checks,
               "calls": calls, "transport": transmissions,
               "evidence_level": "actual configured provider + production assistant leaf; after is review projection only"}
    dump(directory / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-real-provider", action="store_true", required=True)
    parser.add_argument("--cases", nargs="+", choices=[case["name"] for case in CASES])
    args = parser.parse_args()
    out = args.output.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    chosen = [case for case in CASES if args.cases is None or case["name"] in args.cases]
    if any((out / case["name"]).exists() for case in chosen):
        raise SystemExit("A selected case already has evidence. Use a new output directory; no repeated calls.")
    load_local_secrets()
    route = FormalCapabilityRouter().resolve("script_editor")
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "provider": route.provider, "model": route.model,
                "available": route.available, "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
                "source_sha256": {str(path.relative_to(REPO)): sha256(path.read_bytes()).hexdigest() for path in (
                    Path(__file__).resolve(), REPO / "mvp_v2/formal_script_assistant.py", REPO / "mvp_v2/formal_runtime.py",
                    REPO / "mvp_v2/text_providers.py")},
                "scope": "anonymous ten-shot leaf acceptance; no business database or project writes",
                "cases": [case["name"] for case in chosen], "maximum_http_attempts_per_case": 1, "automatic_retries": False}
    dump(out / "manifest.json", manifest)
    (out / "probe-source.py").write_bytes(Path(__file__).read_bytes())
    if not route.available:
        dump(out / "results.json", {"status": "provider_unavailable", "calls": 0})
        raise SystemExit("Configured script editor unavailable; zero calls, no mock replacement.")
    if route.provider != "deepseek-http":
        raise SystemExit("This evidence recorder supports the configured DeepSeek HTTP transport only; no provider substitution.")
    provider = build_text_provider(route.provider)
    shots, context = anonymous_fixture()
    dump(out / "fixture.json", {"shots": shots, "context": context})
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "脚本"
    columns = [key for key in shots[0] if key != "source"]
    worksheet.append(columns)
    for shot in shots:
        worksheet.append([shot[column] for column in columns])
    workbook.save(out / "anonymous-ten-shots.xlsx")
    workbook.close()
    manifest["input_workbook_sha256"] = sha256((out / "anonymous-ten-shots.xlsx").read_bytes()).hexdigest()
    dump(out / "manifest.json", manifest)
    summaries = []
    for case in chosen:
        summary = run_case(case, out=out, route=route, provider=provider, shots=deepcopy(shots), context=deepcopy(context))
        summaries.append(summary)
        dump(out / "results.json", summaries)
        print(json.dumps({key: summary[key] for key in ("case", "result_type", "analysis_code", "changed_shot_numbers")}
                         | {"editor_calls": len(summary["calls"]), "http_attempts": len(summary["transport"])}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
