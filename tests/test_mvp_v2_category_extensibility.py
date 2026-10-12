from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from mvp_v2 import script_type_adapter
from mvp_v2.planning_core import _shot_durations
from mvp_v2.run_contract import sha256_json
from mvp_v2.template_rules import builtin_template_rules


# Captured before the category configuration migration at 177035137bd9a403c37eb4bd29581193eca6921b.
BUILTIN_RULE_HASHES = {
    "launch": "52cb0b5e7716144880fa443b3eb2fef19cde8c8fef64dd1fcb6475aed48c6eb8",
    "exhibition": "678215691f461ab138730794847670e4fb76139dc0235df9a85cd90a3438696b",
    "annual": "6425f14db7f1b6f9cc60ead5e3e5bb7b3eb96d913c2f90d5dbfbd6bec2f47d4d",
    "sports": "8166b19b61047bb20f82f86f8b7c600b6513f1468ed2f57627f52746f08a0842",
    "music_festival": "19c7ddced12da8e2c377359aef786d5a349b46d164f5fe1b0a59ff2d66ada1b8",
    "star_activity": "fb50e519768980f66f6fd29d3f4cbbd900ed1306d3d5277da15fa71ba5ab4795",
}
SCRIPT_TYPES = ("发布会", "展会", "年会", "赛事", "音乐节", "明星活动")


def _events(durations: list[int] | None = None) -> list[dict]:
    events = [
        {"event_id": f"event_{index}", "title": f"匿名活动环节 {index}"}
        for index in range(1, 19)
    ]
    if durations is not None:
        for event, duration in zip(events, durations):
            event["duration_seconds"] = duration
    return events


class CategoryExtensibilityTests(unittest.TestCase):
    def test_seventh_category_loads_from_anonymous_configuration_without_changing_defaults(self):
        baseline = script_type_adapter.SCRIPT_TYPE_METADATA
        source = Path(script_type_adapter.__file__).with_name("category_definitions.json")
        configuration = json.loads(source.read_text(encoding="utf-8"))
        configuration["items"].append({
            "category_id": "anonymous_roadshow",
            "display_name": "匿名路演",
            "source_workbook_name": "匿名路演脚本库.xlsx",
            "source_sheet_names": ["模版", "创意库", "AI_规范表"],
            "dynamic_shot_count": True,
            "workbook_glob": "匿名路演脚本库*.xlsx",
            "aliases": ["anonymous-roadshow"],
            "structural_arcs": [
                [1, 2, "现场建立", ["space", "arrival"]],
                [3, 5, "主题体验", ["interaction", "speech"]],
                [6, 6, "收束", ["closing"]],
            ],
            "pacing_profile": "even",
        })
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "anonymous-categories.json"
            path.write_text(json.dumps(configuration, ensure_ascii=False), encoding="utf-8")
            loaded = script_type_adapter.load_script_type_metadata(path)

        self.assertIsInstance(loaded, tuple)
        self.assertEqual(loaded[:-1], baseline)
        added = loaded[-1]
        self.assertEqual(added.category_id, "anonymous_roadshow")
        self.assertEqual(added.display_name, "匿名路演")
        self.assertEqual(added.aliases, ("anonymous-roadshow",))
        self.assertEqual(added.source_sheet_names, ("模版", "创意库", "AI_规范表"))
        self.assertEqual(added.structural_arcs, (
            (1, 2, "现场建立", ("space", "arrival")),
            (3, 5, "主题体验", ("interaction", "speech")),
            (6, 6, "收束", ("closing",)),
        ))
        self.assertEqual(added.pacing_profile, "even")
        self.assertIs(script_type_adapter.SCRIPT_TYPE_METADATA, baseline)
        self.assertEqual(tuple(item.category_id for item in baseline), tuple(BUILTIN_RULE_HASHES))

    def test_existing_six_category_rule_payloads_keep_baseline_hashes(self):
        for category_id, expected_sha in BUILTIN_RULE_HASHES.items():
            with self.subTest(category_id=category_id):
                self.assertEqual(sha256_json(builtin_template_rules(category_id)), expected_sha)

    def test_launch_keeps_approved_sixty_second_eighteen_shot_budget(self):
        events = _events()
        durations, mode = _shot_durations(
            {"脚本类型": "发布会", "视频时长": "60秒"},
            [[event] for event in events],
            events,
        )

        self.assertEqual(durations, [3, 3, 3, 4, 3, 3, 4, 3, 4, 3, 4, 4, 3, 3, 3, 4, 3, 3])
        self.assertEqual(mode, "approved_launch_60s_budget")

    def test_other_five_categories_evenly_allocate_sixty_seconds_to_eighteen_shots(self):
        for script_type in SCRIPT_TYPES[1:]:
            with self.subTest(script_type=script_type):
                events = _events()
                durations, mode = _shot_durations(
                    {"脚本类型": script_type, "视频时长": "60秒"},
                    [[event] for event in events],
                    events,
                )

                self.assertEqual(durations, [4] * 6 + [3] * 12)
                self.assertEqual(mode, "deterministic_even_budget")

    def test_explicit_event_budgets_override_category_defaults(self):
        explicit_durations = [2, 3, 5] * 6
        for script_type in SCRIPT_TYPES:
            with self.subTest(script_type=script_type):
                events = _events(explicit_durations)
                durations, mode = _shot_durations(
                    {"脚本类型": script_type, "视频时长": "60秒"},
                    [[event] for event in events],
                    events,
                )

                self.assertEqual(durations, explicit_durations)
                self.assertEqual(mode, "explicit_event_budget")


if __name__ == "__main__":
    unittest.main()
