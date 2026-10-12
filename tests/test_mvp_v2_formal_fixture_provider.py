from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from mvp_v2.script_draft import (
    _editor_prompt,
    _ground_primary_events,
    _validate_response,
    _writer_prompt,
)


def _fixture_provider(root: Path):
    """Load the actual fixture functions without booting its server or writing its data."""
    path = Path(__file__).parent / "support/formal_console_fixture_server.py"
    names = {"_provider_business_input", "_deterministic_provider"}
    functions = [node for node in ast.parse(path.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in functions} != names:
        raise AssertionError("The fixture provider functions changed; update this direct-function probe")
    namespace = {"json": json, "root": root}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_deterministic_provider"]


def _source(caption: str) -> dict:
    shots = [
        {"shot_no": "1", "basis": "第1镜，产品演示：工作人员展示产品功能", "planning_contract": {"primary_event": "产品演示", "events": [{"text": "产品演示", "role": "primary"}]}, "editing_logic": {}},
        {"shot_no": "2", "basis": "第2镜，说明发布重点：嘉宾发言", "planning_contract": {"primary_event": "说明发布重点", "events": [{"text": "说明发布重点", "role": "primary"}]}, "editing_logic": {}},
        {"shot_no": "3", "basis": "第3镜，开场造势·人物入画；使用中性环境、构图或节奏动作", "planning_contract": {"primary_event": "开场造势·人物入画", "generated_structure_only": True, "events": [{"event_id": "template_slot_003", "text": "开场造势·人物入画", "role": "primary"}]}, "editing_logic": {"blocked_materials": "签到墙、茶歇、主持人口播", "forbidden_terms": "主持人、演讲者、演讲、茶歇、签到、表演、颁发、颁奖、授牌、启动、合影"}},
    ]
    return {"script_type": "发布会", "shot_count": len(shots), "required_shot_nos": [shot["shot_no"] for shot in shots], "shots": shots, "project_image_context": {"images": [{"caption": caption}, {"caption": "嘉宾沿会场走廊进入活动区域"}]}}


class FormalFixtureProviderTests(unittest.TestCase):
    def test_random_first_image_cannot_empty_a_generated_slot_after_canonical_grounding(self):
        """UUID image ordering must not inject another event into every generated slot."""
        captions = (
            "嘉宾沿会场走廊进入活动区域", "嘉宾在舞台完成颁奖并交接奖杯",
            "主持人在舞台发言", "嘉宾在签到墙前合影", "乐队在舞台表演",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = _fixture_provider(root)
            for caption in captions:
                with self.subTest(first_image=caption):
                    source = _source(caption)
                    plan = SimpleNamespace(script_rows=[{"镜号": str(index)} for index in range(1, 4)])
                    writer = json.loads(provider(_writer_prompt(source), "fixture-writer", 4096))
                    grounded = _ground_primary_events(writer, source)
                    _validate_response(grounded, plan, final=False)
                    self.assertIn(source["shots"][2]["basis"], writer["shots"][2]["visual_description"])
                    self.assertNotIn(caption, writer["shots"][2]["visual_description"])
                    editor = json.loads(provider(_editor_prompt(source, grounded), "fixture-editor", 4096))
                    _validate_response(_ground_primary_events(editor, source), plan, final=True)
                    self.assertIn("产品演示", editor["shots"][0]["visual_description"])
                    self.assertIn("说明发布重点", editor["shots"][1]["visual_description"])
            calls = [json.loads(line) for line in (root / "provider-calls.jsonl").read_text().splitlines()]
            self.assertEqual([call["image_caption"] for call in calls], [caption for caption in captions for _ in range(2)])
            self.assertTrue(all(call["shot_count"] == 3 for call in calls))

    def test_image_order_does_not_change_per_shot_basis(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = _fixture_provider(Path(temporary))
            first = _source("嘉宾在舞台完成颁奖并交接奖杯")
            second = json.loads(json.dumps(first))
            second["project_image_context"]["images"].reverse()
            payloads = [json.loads(provider(_writer_prompt(source), "fixture-writer", 4096)) for source in (first, second)]
            self.assertEqual(payloads[0]["shots"], payloads[1]["shots"])
