from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from mvp_v2.script_draft import create_script_draft


class DualModelRouteTests(unittest.TestCase):
    def test_writer_and_editor_use_separate_provider_and_model(self):
        plan = _plan()
        calls = []

        def writer(prompt, model, max_tokens):
            calls.append(("writer", model, max_tokens))
            return json.dumps(_response("初稿"), ensure_ascii=False)

        def editor(prompt, model, max_tokens):
            calls.append(("editor", model, max_tokens))
            return json.dumps(_response("终稿"), ensure_ascii=False)

        with tempfile.TemporaryDirectory() as tmp:
            drafted = create_script_draft(
                plan,
                quality_profile="client-review",
                text_model="legacy",
                text_command="mmx",
                cache_dir=Path(tmp),
                writer_provider=writer,
                editor_provider=editor,
                writer_provider_name="deepseek-http",
                writer_model="deepseek-v4-pro",
                editor_provider_name="minimax-cli",
                editor_model="MiniMax-M2.7-highspeed",
            )

        self.assertEqual([item[:2] for item in calls], [
            ("writer", "deepseek-v4-pro"),
            ("editor", "MiniMax-M2.7-highspeed"),
        ])
        self.assertEqual(drafted["usage"]["live_call_count"], 2)
        self.assertEqual(drafted["writer_provider"], "deepseek-http")
        self.assertEqual(drafted["editor_provider"], "minimax-cli")


def _plan():
    script = {
        "镜号": "1", "结构": "开场", "时长(秒)": 3, "效果": "直切",
        "类型": "实拍", "景别": "全景", "运镜": "推进", "画面描述": "原始画面",
        "声音": "音乐起",
    }
    detail = {
        "槽位功能": "建立现场", "原始画面描述": "已审核的发布会现场建立镜头",
        "槽位选择原因": "开场建立", "允许素材": "会场", "禁止素材": "竞品",
        "推荐景别": "全景", "推荐运镜": "推进", "推荐声音": "音乐起",
        "节奏说明": "由慢入快", "必须视觉证据": "会场", "重复素材规则": "不重复",
        "采用来源证据": "文字依据",
    }
    return SimpleNamespace(
        script_rows=[script], detail_rows=[detail],
        source_refs=[{"source_file": "/source/library.xlsx", "source_sheet": "模版", "source_row": 7}],
        brief={"项目名称": "测试发布会", "品牌或客户": "测试品牌", "行业或活动类别": "科技", "风格调性": "克制", "必须出现的内容": "会场", "禁忌或不能出现的内容": "竞品"},
        constraints={},
    )


def _response(prefix: str):
    return {
        "schema_version": "mplus-script-draft-response/v1",
        "overall_summary": f"{prefix}项目专属说明",
        "shots": [{
            "shot_no": "1",
            "visual_description": f"{prefix}镜头从会场全景稳定推进并明确呈现当前品牌发布活动现场氛围",
            "sound": "音乐渐起",
            "voiceover": "",
            "subtitle": "",
            "pending_confirmations": [],
        }],
    }


if __name__ == "__main__":
    unittest.main()
