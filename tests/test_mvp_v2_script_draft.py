import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mvp_v2.script_draft import (
    MAX_OUTPUT_TOKENS,
    SCHEMA_VERSION,
    ScriptDraftError,
    ScriptDraftProviderError,
    _editor_prompt,
    _extract_text,
    _ground_primary_events,
    _ground_required_assignments,
    _ground_special_requirements,
    _model_source,
    _normalize_client_placeholders,
    _normalize_pending_confirmations,
    _parse_response,
    _route_required_content_to_indexed_visuals,
    _writer_prompt,
    create_script_draft,
)


class ScriptDraftTests(unittest.TestCase):
    def test_model_source_does_not_present_structure_label_as_brief_requirement(self):
        plan = self._plan()
        plan.detail_rows[0].update({
            "规划主事件": "核心节奏·中景承接",
            "缺口说明": "Brief规划内容：派发花茶",
        })
        plan.shot_plans = [{
            "shot_no": "1", "primary_event": "核心节奏·中景承接",
            "generated_structure_only": True,
            "events": [
                {"event_id": "template_slot_001", "text": "核心节奏·中景承接", "role": "primary"},
                {"event_id": "brief_001", "text": "派发花茶", "role": "secondary"},
            ],
        }]

        self.assertEqual(_model_source(plan)["shots"][0]["required_brief_content"], ["派发花茶"])

    def test_structural_placeholder_does_not_overwrite_or_append_to_real_action(self):
        payload = {"shots": [
            {"shot_no": "26", "visual_description": "粉丝举起手机闪光灯，舞台灯光掠过人群。"},
            {"shot_no": "27", "visual_description": "镜头缓缓推进，捕捉观众微笑与挥手回应。"},
        ]}
        source = {"shots": [{
            "shot_no": str(number),
            "planning_contract": {
                "primary_event": f"核心节奏·{phase}",
                "generated_structure_only": True,
                "events": [{"event_id": f"template_slot_{number:03d}",
                            "text": f"核心节奏·{phase}", "role": "primary"}],
            },
        } for number, phase in [(26, "人物入画"), (27, "中景承接")]]}
        self.assertEqual(_ground_primary_events(payload, source), payload)

    def test_prompts_lock_dynamic_shot_count_and_numbers(self):
        source = _model_source(self._plan())
        draft = self._response("当前项目可执行画面")

        self.assertEqual(source["shot_count"], 1)
        self.assertEqual(source["required_shot_nos"], ["1"])
        self.assertIn("shots 必须且只能输出1项", _writer_prompt(source))
        self.assertIn("shot_no 必须按['1']", _writer_prompt(source))
        self.assertIn("shots 必须且只能保留1项", _editor_prompt(source, draft))
        self.assertIn("blocked_materials", _writer_prompt(source))
        self.assertIn("直接重写", _editor_prompt(source, draft))
        self.assertIn("结构占位", _writer_prompt(source))
        self.assertIn("画面与声音不得同时完全相同", _editor_prompt(source, draft))
        self.assertIn("voiceover 与 subtitle 必须输出字符串", _writer_prompt(source))
        self.assertIn('"voiceover"', _writer_prompt(source))
        self.assertIn('"subtitle"', _editor_prompt(source, draft))

    def test_provider_voiceover_and_subtitle_are_written_into_script_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            def provider(_prompt, _model, _max_tokens):
                response = self._response("当前项目可执行画面")
                response["shots"][0]["voiceover"] = "欢迎来到真实活动现场"
                response["shots"][0]["subtitle"] = "活动正式开始"
                return json.dumps(response, ensure_ascii=False)

            result = create_script_draft(
                self._plan(),
                quality_profile="client-review",
                text_model="deepseek-v4-flash",
                text_command="",
                cache_dir=tmp,
                provider=provider,
            )

        self.assertEqual(result["script_rows"][0]["旁白"], "欢迎来到真实活动现场")
        self.assertEqual(result["script_rows"][0]["字幕"], "活动正式开始")

    def test_provider_must_return_voiceover_and_subtitle_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            def provider(_prompt, _model, _max_tokens):
                response = self._response("当前项目可执行画面")
                response["shots"][0].pop("voiceover")
                return json.dumps(response, ensure_ascii=False)

            with self.assertRaisesRegex(ScriptDraftError, "缺少 voiceover 字段"):
                create_script_draft(
                    self._plan(),
                    quality_profile="client-review",
                    text_model="deepseek-v4-flash",
                    text_command="",
                    cache_dir=tmp,
                    provider=provider,
                )

    def test_extracts_mmx_anthropic_content_blocks(self):
        raw = json.dumps({
            "content": [
                {"type": "thinking", "thinking": "internal"},
                {"type": "text", "text": '{"ok":true}'},
            ]
        })

        self.assertEqual(_extract_text(raw), '{"ok":true}')

    def test_missing_final_text_reports_reasoning_exhaustion_evidence(self):
        raw = json.dumps({
            "content": [{"type": "thinking", "thinking": "internal"}],
            "usage": {"output_tokens": 4096},
            "stop_reason": "max_tokens",
        })

        with self.assertRaisesRegex(
            ScriptDraftError,
            "stop_reason=max_tokens.*content_types=\\['thinking'\\].*output_tokens=4096",
        ):
            _extract_text(raw)

    def test_draft_profile_never_calls_real_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = create_script_draft(
                self._plan(),
                quality_profile="draft",
                text_model="MiniMax-M2.7",
                text_command="mmx",
                cache_dir=tmp,
                provider=lambda *args: (_ for _ in ()).throw(
                    AssertionError("默认测试不得调用真实文本模型")
                ),
            )

        self.assertEqual(result["usage"]["live_call_count"], 0)
        self.assertEqual(result["script_rows"][0]["画面描述"], "已审核参考画面")

    def test_client_review_calls_writer_and_editor_once_then_hits_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def provider(prompt, model, max_tokens):
                calls.append((prompt, model, max_tokens))
                return json.dumps(self._response("当前项目可执行画面"), ensure_ascii=False)

            first = create_script_draft(
                self._plan(),
                quality_profile="client-review",
                text_model="MiniMax-M2.7",
                text_command="mmx",
                cache_dir=tmp,
                provider=provider,
            )
            second = create_script_draft(
                self._plan(),
                quality_profile="client-review",
                text_model="MiniMax-M2.7",
                text_command="mmx",
                cache_dir=tmp,
                provider=lambda *args: (_ for _ in ()).throw(
                    AssertionError("相同输入应完全命中缓存")
                ),
            )
            ledger = [
                json.loads(line)
                for line in (Path(tmp) / "text_call_ledger.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        self.assertEqual(len(calls), 2)
        self.assertEqual(MAX_OUTPUT_TOKENS, 65536)
        self.assertTrue(all(call[2] == 65536 for call in calls))
        self.assertEqual(first["usage"]["live_call_count"], 2)
        self.assertEqual(second["usage"]["live_call_count"], 0)
        self.assertEqual(second["usage"]["cache_hit_count"], 2)
        self.assertEqual(
            [row["event"] for row in ledger],
            [
                "live_call_succeeded",
                "live_call_succeeded",
                "cache_hit",
                "cache_hit",
            ],
        )
        self.assertTrue(all(row["model"] == "MiniMax-M2.7" for row in ledger))
        self.assertTrue(all(row["prompt_sha256"] for row in ledger))
        self.assertTrue(all(row["response_sha256"] for row in ledger))
        self.assertEqual(first["usage"]["asset_logic_coverage"], 1.0)

    def test_diagnostics_separate_source_raw_processing_and_final_output(self):
        from hashlib import sha256
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def provider(prompt, model, max_tokens):
                raw = json.dumps(self._response('写作原稿' if not calls else '编辑终稿'),
                                 ensure_ascii=False)
                calls.append((prompt, raw))
                return raw
            plan = self._plan()
            result = create_script_draft(plan, quality_profile='client-review',
                text_model='MiniMax-M2.7', text_command='mmx', cache_dir=tmp,
                provider=provider)
            records = [json.loads(path.read_text())
                       for path in (Path(tmp) / 'diagnostics').glob('*.json')]
            self.assertEqual({row['pass_name'] for row in records}, {'writer', 'editor'})
            self.assertEqual(len(records), 2)
            for index, pass_name in enumerate(('writer', 'editor')):
                record = next(row for row in records if row['pass_name'] == pass_name)
                prompt, raw = calls[index]
                self.assertEqual(record['raw_response'], raw)
                self.assertEqual(record['stored_response_sha256'], sha256(raw.encode()).hexdigest())
                self.assertEqual(record['invocation']['prompt'], prompt)
                self.assertFalse(record['cache_hit'])
                self.assertEqual(record['status'], 'succeeded')
                stages = record['stages']
                self.assertEqual(stages[0]['input'], raw)
                self.assertEqual(stages[0]['output'], json.loads(raw))
                for previous, current in zip(stages, stages[1:]):
                    self.assertEqual(previous['output'], current['input'])
                self.assertEqual(stages[-1]['stage'], 'validation')
                self.assertTrue(all(stage['status'] == 'succeeded' for stage in stages))
            writer_source = json.loads(calls[0][0].split('\nDATA:', 1)[1])
            self.assertEqual(writer_source, _model_source(plan))
            editor = next(row for row in records if row['pass_name'] == 'editor')
            final_shots = editor['stages'][-1]['input']['shots']
            self.assertEqual([row['画面描述'] for row in result['script_rows']],
                             [shot['visual_description'].strip() for shot in final_shots])
            self.assertEqual(result['usage']['live_call_count'], 2)

    def test_editor_may_omit_only_schema_version_but_not_override_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = 0

            def provider(prompt, model, max_tokens):
                nonlocal calls
                calls += 1
                payload = self._response("当前项目可执行画面")
                if calls == 2:
                    payload.pop("schema_version")
                return json.dumps(payload, ensure_ascii=False)

            result = create_script_draft(
                self._plan(),
                quality_profile="client-review",
                text_model="deepseek-v4-flash",
                text_command="",
                cache_dir=tmp,
                provider=provider,
            )

        self.assertEqual(calls, 2)
        self.assertEqual(len(result["script_rows"]), 1)

    def test_model_source_contains_auditable_asset_editing_logic(self):
        plan = self._plan()
        plan.detail_rows[0]["缺口说明"] = "Brief规划内容：赛事标识，须由已审核来源提供证据"
        plan.detail_rows[0]["逻辑槽位"] = 1
        plan.detail_rows[0]["模板槽位总数"] = 18
        source = _model_source(plan)
        shot = source["shots"][0]

        self.assertEqual(source["editing_logic_contract"]["source_kind"], "approved_asset_library")
        self.assertEqual(source["editing_logic_contract"]["structure_arc"], ["开场"])
        self.assertEqual(shot["camera_type"], "航拍")
        self.assertEqual(shot["shot_size"], "全景")
        self.assertEqual(shot["camera_move"], "缓慢推进")
        self.assertEqual(shot["editing_logic"]["rhythm"], "快速建立品牌识别")
        self.assertEqual(shot["editing_logic"]["repeat_policy"], "片头只出现一次")
        self.assertIn("主持人", shot["editing_logic"]["forbidden_terms"])
        self.assertEqual(shot["required_brief_content"], ["赛事标识"])
        self.assertEqual(shot["source"], "模版!7")

    def test_model_source_preserves_special_audio_requirement_outside_visual_content(self):
        plan = self._plan()
        plan.brief["特殊需求"] = "需要以明星的歌曲作为视频背景音乐"

        source = _model_source(plan)

        self.assertEqual(
            source["brief"]["特殊需求"],
            "需要以明星的歌曲作为视频背景音乐",
        )
        self.assertEqual(source["shots"][0]["required_brief_content"], [])

    def test_special_star_music_is_grounded_in_sound_with_human_confirmation(self):
        payload = self._response("已审核参考画面")
        payload["shots"][0]["sound"] = "粉丝欢呼声"
        source = _model_source(self._plan())
        source["brief"]["特殊需求"] = "需要以明星的歌曲作为视频背景音乐"

        grounded = _ground_special_requirements(payload, source)

        self.assertEqual(grounded["shots"][0]["sound"], "明星歌曲BGM+粉丝欢呼")
        self.assertEqual(
            grounded["shots"][0]["pending_confirmations"],
            ["具体歌曲及授权待确认"],
        )

    def test_model_source_locks_plan_event_without_using_gap_note(self):
        plan = self._plan()
        plan.detail_rows[0]["规划主事件"] = "产品互动"
        plan.detail_rows[0]["缺口说明"] = ""
        plan.shot_plans = [{
            "shot_no": "1",
            "narrative_stage": "development",
            "primary_event": "产品互动",
            "event_ids": ["event_001"],
            "events": [
                {"event_id": "event_001", "text": "产品互动", "role": "primary"},
                {"event_id": "event_002", "text": "粉丝应援", "role": "secondary"},
            ],
            "requirement_ids": ["mandatory_required_001"],
            "brief_evidence": {"text": "Brief明确要求产品互动"},
            "functional_source": {
                "source_file": "已审核模板.xlsx",
                "source_sheet": "模版",
                "source_row": 7,
            },
            "functional_reference_notice": "仅作镜头功能参考，非本项目事实或素材",
            "excluded_source_mechanisms": ["剪彩"],
            "prohibited_inheritance": "不得继承未确认案例机制",
            "transition_reason": "承接空间建立并转入落版",
        }]

        shot = _model_source(plan)["shots"][0]

        self.assertEqual(shot["required_brief_content"], ["产品互动"])
        self.assertTrue(shot["planning_contract"]["locked"])
        self.assertEqual(
            [item["text"] for item in shot["planning_contract"]["events"]],
            ["产品互动", "粉丝应援"],
        )
        self.assertEqual(
            shot["planning_contract"]["requirement_ids"],
            ["mandatory_required_001"],
        )
        self.assertEqual(
            shot["planning_contract"]["excluded_source_mechanisms"],
            ["剪彩"],
        )

    def test_model_source_receives_indexed_visual_candidates(self):
        source = _model_source(
            self._plan(),
            image_manifest_rows=[{
                "image_id": "img_stage",
                "原图路径": "/tmp/stage.png",
                "asset_role": "source_reference",
                "来源文件": "已审核模板.xlsx",
                "来源 sheet": "模版",
                "来源行": 7,
            }],
            image_analysis_rows=[{
                "image_id": "img_stage",
                "caption": "品牌主舞台与观众全景",
                "tags": ["品牌标识", "主舞台", "观众"],
                "brand_names": ["无"],
                "source": "fixture",
            }],
        )

        candidates = source["shots"][0]["indexed_visual_candidates"]
        self.assertEqual(candidates[0]["image_id"], "img_stage")
        self.assertIn("主舞台", candidates[0]["summary"])

    def test_required_content_moves_to_shot_with_indexed_visual_evidence(self):
        rows = [{
            "shot_no": "1",
            "structure": "开场氛围",
            "function": "邀请函入场",
            "basis": "人群穿过入口",
            "source_evidence": "邀请函入场",
            "required_brief_content": ["比赛对抗"],
            "indexed_visual_candidates": [{
                "score": 90,
                "summary": "普通入口人群",
                "tags": ["入口"],
            }],
        }, {
            "shot_no": "2",
            "structure": "竞技瞬间",
            "function": "参赛者群像",
            "basis": "赛场选手情绪",
            "source_evidence": "电竞选手与观众",
            "required_brief_content": [],
            "indexed_visual_candidates": [{
                "score": 84,
                "summary": "电竞比赛现场，玩家正在对战",
                "tags": ["电竞", "比赛", "玩家"],
            }],
        }]

        _route_required_content_to_indexed_visuals(rows)

        self.assertEqual(rows[0]["required_brief_content"], [])
        self.assertEqual(rows[1]["required_brief_content"], ["比赛对抗"])

    def test_required_content_stays_when_assigned_source_already_supports_it(self):
        rows = [{
            "shot_no": "1",
            "structure": "品牌落版",
            "function": "颁奖与场馆高潮",
            "basis": "切入颁奖仪式与观众欢呼",
            "source_evidence": "已审核来源包含颁奖时刻",
            "required_brief_content": ["冲线或颁奖"],
            "indexed_visual_candidates": [],
        }, {
            "shot_no": "2",
            "structure": "竞技瞬间",
            "function": "运动员情绪",
            "basis": "运动员表情",
            "source_evidence": "运动员奔跑",
            "required_brief_content": [],
            "indexed_visual_candidates": [{
                "score": 90,
                "summary": "跑者冲过终点线",
                "tags": ["冲线", "跑者"],
            }],
        }]

        _route_required_content_to_indexed_visuals(rows)

        self.assertEqual(rows[0]["required_brief_content"], ["冲线或颁奖"])
        self.assertEqual(rows[1]["required_brief_content"], [])

    def test_required_content_moves_to_other_approved_source_evidence(self):
        rows = [{
            "shot_no": "4",
            "structure": "装置空镜",
            "function": "宏大氛围建立",
            "basis": "装置空镜与主会场光束",
            "source_evidence": "已审核来源包含装置空镜和主题字样",
            "required_brief_content": [],
            "indexed_visual_candidates": [],
        }, {
            "shot_no": "7",
            "structure": "人气与现场互动",
            "function": "产品体验",
            "basis": "生成稿声称主题装置与产品体验区材质细节",
            "source_evidence": "已审核来源包含产品互动体验",
            "required_brief_content": ["主题装置"],
            "indexed_visual_candidates": [],
        }]

        _route_required_content_to_indexed_visuals(rows)

        self.assertEqual(rows[0]["required_brief_content"], ["主题装置"])
        self.assertEqual(rows[1]["required_brief_content"], [])

    def test_grounding_preserves_original_even_with_requirement_or_asset_evidence(self):
        payload = {
            "schema_version": SCHEMA_VERSION,
            "overall_summary": "赛事故事",
            "shots": [{
                "shot_no": "1",
                "visual_description": "特写选手紧张表情",
                "sound": "心跳声",
                "pending_confirmations": [],
            }],
        }
        source = {"shots": [{
            "shot_no": "1",
            "basis": "选手情绪",
            "source_evidence": "电竞赛事参赛者",
            "required_brief_content": ["比赛对抗"],
            "indexed_visual_candidates": [{
                "score": 84,
                "summary": "电竞比赛现场，玩家正在对战",
                "tags": ["电竞", "比赛", "玩家"],
            }],
        }]}

        grounded = _ground_required_assignments(payload, source)

        self.assertEqual(payload, grounded)

        source["shots"][0]["indexed_visual_candidates"] = []
        unsupported = _ground_required_assignments(payload, source)
        self.assertNotIn("比赛对抗", unsupported["shots"][0]["visual_description"])

        source["shots"][0]["basis"] = "生成稿声称比赛对抗已经发生"
        unsupported_basis = _ground_required_assignments(payload, source)
        self.assertNotIn("比赛对抗", unsupported_basis["shots"][0]["visual_description"])

        source["confirmed_required_brief_content"] = ["比赛对抗"]
        confirmed = _ground_required_assignments(payload, source)
        self.assertEqual(payload, confirmed)

    def test_missing_primary_event_does_not_replace_model_text(self):
        payload = {
            "schema_version": SCHEMA_VERSION,
            "overall_summary": "发布会流程",
            "shots": [{
                "shot_no": "1",
                "visual_description": "国外代表发言，观众鼓掌。",
                "sound": "发言同期声",
                "pending_confirmations": [],
            }],
        }
        source = {"shots": [{
            "shot_no": "1",
            "planning_contract": {
                "narrative_stage": "development",
                "primary_event": "各地区代表颁奖仪式",
                "events": [{
                    "event_id": "event_009",
                    "text": "各地区代表颁奖仪式",
                    "role": "primary",
                }],
            },
        }]}

        grounded = _ground_primary_events(payload, source)

        self.assertEqual(payload, grounded)

    def test_primary_event_replaces_cross_shot_shorthand_and_blocked_material(self):
        payload = {
            "shots": [{
                "shot_no": "10",
                "visual_description": "国外代表6发言，中景捕捉演讲者。",
            }, {
                "shot_no": "12",
                "visual_description": "内场空镜，观众席座无虚席。",
            }],
        }
        source = {"shots": [{
            "shot_no": "10",
            "editing_logic": {"blocked_materials": "演讲者，品牌落版"},
            "planning_contract": {
                "primary_event": "国外地区各代表颁奖仪式",
                "events": [{
                    "event_id": "event_009",
                    "text": "国外地区各代表颁奖仪式",
                    "role": "primary",
                }],
            },
        }, {
            "shot_no": "11",
            "planning_contract": {
                "primary_event": "6.国外代表6发言(全景+中景)",
                "events": [],
            },
        }, {
            "shot_no": "12",
            "editing_logic": {"blocked_materials": "观众反应，外场建筑"},
            "planning_contract": {
                "primary_event": "内场空镜展示",
                "events": [{
                    "event_id": "mandatory_required_002",
                    "text": "内场空镜展示",
                    "role": "primary",
                }],
            },
        }]}

        grounded = _ground_primary_events(payload, source)

        self.assertIn("国外地区各代表颁奖仪式", grounded["shots"][0]["visual_description"])
        self.assertNotIn("演讲者", grounded["shots"][0]["visual_description"])
        self.assertNotIn("观众", grounded["shots"][1]["visual_description"])
        self.assertIn("内场空镜展示", grounded["shots"][1]["visual_description"])

    def test_authoritative_slot_forbidden_terms_are_grounded_before_audit(self):
        payload = {"shots": [{
            "shot_no": "3",
            "visual_description": "主持人登台并带来节目表演。",
            "sound": "主持人报幕",
        }]}
        source = {"shots": [{
            "shot_no": "3",
            "sound_basis": "飞行呼啸",
            "editing_logic": {
                "blocked_materials": "签到墙、茶歇",
                "forbidden_terms": "主持人、表演、签到",
            },
            "planning_contract": {
                "primary_event": "品牌视觉转场",
                "events": [{
                    "event_id": "event_003",
                    "text": "品牌视觉转场",
                    "role": "primary",
                }],
            },
        }]}

        grounded = _ground_primary_events(payload, source)["shots"][0]

        self.assertIn("品牌视觉转场", grounded["visual_description"])
        self.assertNotIn("主持人", grounded["visual_description"])
        self.assertNotIn("表演", grounded["visual_description"])
        self.assertEqual(grounded["sound"], "飞行呼啸")

    def test_confirmed_same_shot_event_can_override_slot_forbidden_term(self):
        payload = {"shots": [{
            "shot_no": "17",
            "visual_description": "片尾落版完成整片收尾。",
            "sound": "音乐收束",
        }]}
        source = {"shots": [{
            "shot_no": "17",
            "editing_logic": {"forbidden_terms": "落版"},
            "planning_contract": {
                "primary_event": "品牌收束",
                "events": [{
                    "event_id": "event_017",
                    "text": "品牌收束",
                    "role": "primary",
                }],
            },
        }]}

        grounded = _ground_primary_events(payload, source)["shots"][0]

        self.assertIn("落版", grounded["visual_description"])

    def test_client_placeholder_is_replaced_with_specific_generic_copy(self):
        normalized = _normalize_client_placeholders({
            "shots": [{
                "shot_no": "1",
                "visual_description": "大屏同步展示核心内容。",
            }],
        })

        self.assertEqual(
            normalized["shots"][0]["visual_description"],
            "大屏同步展示演讲要点。",
        )

    def test_missing_opening_requirement_is_not_invented(self):
        payload = {
            "schema_version": SCHEMA_VERSION,
            "overall_summary": "活动回顾",
            "shots": [{
                "shot_no": "1",
                "visual_description": "情侣开始互动挑战。",
                "sound": "音乐起",
                "pending_confirmations": [],
            }],
        }
        source = {
            "confirmed_required_brief_content": ["开头AI蝴蝶转场"],
            "shots": [{
                "shot_no": "1",
                "source_evidence": "",
                "required_brief_content": ["开头AI蝴蝶转场"],
                "indexed_visual_candidates": [],
                "planning_contract": {"narrative_stage": "opening"},
            }],
        }

        grounded = _ground_required_assignments(payload, source)

        self.assertEqual(payload, grounded)

    def test_prompts_make_plan_events_and_review_feedback_authoritative(self):
        source = _model_source(self._plan())
        source["revision_constraints"] = [{
            "scope": "shot", "shot_no": "1", "category": "structure",
            "comment": "开头应该是空镜展示", "desired_change": "",
        }]

        writer = _writer_prompt(source)
        editor = _editor_prompt(source, self._response("原始草稿"))

        self.assertIn("planning_contract.primary_event", writer)
        self.assertIn("revision_constraints", writer)
        self.assertIn("不得在中段重启开场", writer)
        self.assertIn("planning_contract.primary_event", editor)
        self.assertIn("revision_constraints", editor)

    def test_client_review_blocks_incomplete_asset_editing_logic(self):
        plan = self._plan()
        plan.script_rows[0]["运镜"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ScriptDraftError, "剪辑逻辑不完整.*camera_move"):
                create_script_draft(
                    plan,
                    quality_profile="client-review",
                    text_model="MiniMax-M2.7",
                    text_command="mmx",
                    cache_dir=tmp,
                    provider=lambda *args: "",
                )

    def test_model_cannot_output_locked_source_triplet(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = self._response("项目画面")
            response["shots"][0]["source_ref"] = {"source_row": 999}
            with self.assertRaisesRegex(ScriptDraftError, "不可变字段"):
                create_script_draft(
                    self._plan(),
                    quality_profile="client-review",
                    text_model="MiniMax-M2.7",
                    text_command="mmx",
                    cache_dir=tmp,
                    provider=lambda *args: json.dumps(response, ensure_ascii=False),
                )

    def test_editor_failure_retry_reuses_writer_cache_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            responses = [
                json.dumps(self._response("写作稿"), ensure_ascii=False),
                "not-json",
            ]
            with self.assertRaisesRegex(ScriptDraftError, "合法 JSON"):
                create_script_draft(
                    self._plan(),
                    quality_profile="client-review",
                    text_model="MiniMax-M2.7",
                    text_command="mmx",
                    cache_dir=tmp,
                    provider=lambda *args: responses.pop(0),
                )

            calls = []
            result = create_script_draft(
                self._plan(),
                quality_profile="client-review",
                text_model="MiniMax-M2.7",
                text_command="mmx",
                cache_dir=tmp,
                provider=lambda *args: calls.append(True) or json.dumps(
                    self._response("编辑后稿件"), ensure_ascii=False
                ),
            )

        self.assertEqual(calls, [True])
        self.assertTrue(result["usage"]["writer_cache_hit"])
        self.assertFalse(result["usage"]["editor_cache_hit"])
        self.assertEqual(result["usage"]["live_call_count"], 1)

    def test_missing_comma_before_known_schema_key_is_repaired_locally(self):
        raw = (
            '{"schema_version":"mplus-script-draft-response/v1",'
            '"overall_summary":"summary",'
            '"shots":[{"shot_no":"1","visual_description":"frame" '
            '"sound":"music","pending_confirmations":[]}]}'
        )

        payload = _parse_response(raw)

        self.assertEqual(payload["shots"][0]["visual_description"], "frame")
        self.assertEqual(payload["shots"][0]["sound"], "music")

    def test_missing_comma_between_shot_objects_is_repaired_locally(self):
        raw = (
            '{"schema_version":"mplus-script-draft-response/v1",'
            '"overall_summary":"summary","shots":['
            '{"shot_no":"1","visual_description":"a","sound":"a",'
            '"pending_confirmations":[]}'
            '{"shot_no":"2","visual_description":"b","sound":"b",'
            '"pending_confirmations":[]}]}'
        )

        payload = _parse_response(raw)

        self.assertEqual([shot["shot_no"] for shot in payload["shots"]], ["1", "2"])

    def test_unescaped_ascii_quotes_inside_known_text_value_are_repaired_locally(self):
        raw = (
            '{"schema_version":"mplus-script-draft-response/v1",'
            '"overall_summary":"Midea"智美万象"主题发布会",'
            '"shots":[{"shot_no":"1","visual_description":"主屏显示"智美万象"主题",'
            '"sound":"music","pending_confirmations":[]}]}'
        )

        payload = _parse_response(raw)

        self.assertEqual(payload["overall_summary"], 'Midea"智美万象"主题发布会')
        self.assertEqual(
            payload["shots"][0]["visual_description"],
            '主屏显示"智美万象"主题',
        )

    def test_unescaped_quote_repair_does_not_apply_to_schema_or_unknown_fields(self):
        with self.assertRaisesRegex(ScriptDraftError, "合法 JSON"):
            _parse_response('{"schema_version":"ok"非法"值","unknown":"value"}')

    def test_local_json_repair_is_audited_without_extra_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def provider(prompt, model, max_tokens):
                calls.append(prompt)
                valid = json.dumps(self._response("编辑稿"), ensure_ascii=False)
                if len(calls) == 1:
                    return valid.replace('", "sound"', '" "sound"', 1)
                return valid

            result = create_script_draft(
                self._plan(),
                quality_profile="client-review",
                text_model="MiniMax-M2.7",
                text_command="mmx",
                cache_dir=tmp,
                provider=provider,
            )
            ledger = [
                json.loads(line)
                for line in (Path(tmp) / "text_call_ledger.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["usage"]["live_call_count"], 2)
        self.assertEqual(result["usage"]["writer_json_repair_count"], 1)
        self.assertEqual(result["usage"]["editor_json_repair_count"], 0)
        self.assertEqual(result["usage"]["local_json_repair_count"], 1)
        self.assertEqual(
            [row["event"] for row in ledger],
            ["live_call_succeeded", "response_json_repaired", "live_call_succeeded"],
        )

    def test_json_repair_does_not_rewrite_unknown_or_ambiguous_content(self):
        with self.assertRaisesRegex(ScriptDraftError, "合法 JSON"):
            _parse_response('{"schema_version":"ok" "unknown":"value"}')

    def test_failed_live_call_is_recorded_without_creating_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ScriptDraftError, "provider failed"):
                create_script_draft(
                    self._plan(),
                    quality_profile="client-review",
                    text_model="MiniMax-M2.7",
                    text_command="mmx",
                    cache_dir=tmp,
                    provider=lambda *args: (_ for _ in ()).throw(
                        ScriptDraftError("provider failed")
                    ),
                )

            ledger = [
                json.loads(line)
                for line in (Path(tmp) / "text_call_ledger.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            cache_files = [
                path for path in Path(tmp).glob("*.json")
                if path.name != "text_call_ledger.jsonl"
            ]

        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["event"], "live_call_failed")
        self.assertEqual(ledger[0]["error_type"], "ScriptDraftError")
        self.assertEqual(cache_files, [])


    def test_empty_sound_is_rejected_before_caching_and_retry_can_use_a_complete_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._response('编辑稿')
            payload['shots'][0]['sound'] = ''
            with self.assertRaisesRegex(ScriptDraftError, '声音.*空'):
                create_script_draft(self._plan(), quality_profile='client-review', text_model='anonymous',
                    text_command='unused', cache_dir=tmp, provider=lambda *args: json.dumps(payload, ensure_ascii=False))
            self.assertFalse(list(Path(tmp).glob('*.json')))
            calls = []
            def complete(prompt, model, limit):
                calls.append(prompt)
                return json.dumps(self._response('完整编辑稿'), ensure_ascii=False)
            result = create_script_draft(self._plan(), quality_profile='client-review', text_model='anonymous',
                text_command='unused', cache_dir=tmp, provider=complete)
            self.assertEqual(len(calls), 2)
            self.assertTrue(result['script_rows'][0]['声音'])

    def test_mmx_provider_uses_message_file_instead_of_stdin(self):
        from mvp_v2.script_draft import _mmx_provider

        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"content": [{"type": "text", "text": '{"ok":true}'}]}),
            stderr="",
        )
        with patch("mvp_v2.script_draft.subprocess.run", return_value=completed) as run:
            result = _mmx_provider("mmx")("hello", "MiniMax-M2.7", 65536)

        command = run.call_args.args[0]
        message_path = command[command.index("--messages-file") + 1]
        self.assertNotEqual(message_path, "-")
        self.assertEqual(command[command.index("--timeout") + 1], "600")
        self.assertNotIn("input", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["timeout"], 630)
        self.assertEqual(result, '{"ok":true}')

    def test_mmx_provider_failure_is_retryable_provider_error(self):
        from mvp_v2.script_draft import _mmx_provider

        completed = SimpleNamespace(returncode=1, stdout="", stderr="temporary failure")
        with patch("mvp_v2.script_draft.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ScriptDraftProviderError, "temporary failure"):
                _mmx_provider("mmx")("hello", "MiniMax-M2.7", 65536)

    def test_mmx_provider_retries_transient_overload_without_human_intervention(self):
        from mvp_v2.script_draft import _mmx_provider

        overloaded = SimpleNamespace(returncode=1, stdout="", stderr="HTTP 529 overloaded_error")
        succeeded = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"content": [{"type": "text", "text": '{"ok":true}'}]}),
            stderr="",
        )
        with (
            patch(
                "mvp_v2.script_draft.subprocess.run",
                side_effect=[overloaded, overloaded, succeeded],
            ) as run,
            patch("mvp_v2.script_draft.time.sleep") as sleep,
        ):
            result = _mmx_provider("mmx")("hello", "MiniMax-M2.7", 65536)

        self.assertEqual(result, '{"ok":true}')
        self.assertEqual(run.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15])

    def test_final_editor_output_rejects_internal_process_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            responses = [
                json.dumps(self._response("正常写作稿"), ensure_ascii=False),
                json.dumps(self._response("镜头明确呈现Brief要求"), ensure_ascii=False),
            ]
            with self.assertRaisesRegex(ScriptDraftError, "内部过程语言"):
                create_script_draft(
                    self._plan(),
                    quality_profile="client-review",
                    text_model="MiniMax-M2.7",
                    text_command="mmx",
                    cache_dir=tmp,
                    provider=lambda *args: responses.pop(0),
                )

    def test_final_editor_output_rejects_ai_production_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            responses = [
                json.dumps(self._response("正常写作稿"), ensure_ascii=False),
                json.dumps(self._response("AI合成主舞台画面"), ensure_ascii=False),
            ]
            with self.assertRaisesRegex(ScriptDraftError, "内部过程语言"):
                create_script_draft(
                    self._plan(),
                    quality_profile="client-review",
                    text_model="MiniMax-M3",
                    text_command="mmx",
                    cache_dir=tmp,
                    provider=lambda *args: responses.pop(0),
                )

    def test_pending_confirmation_normalization_removes_execution_details_and_caps_total(self):
        payload = {
            "shots": [
                {
                    "shot_no": str(index),
                    "pending_confirmations": ["字体样式", f"关键事实{index}"],
                }
                for index in range(1, 9)
            ],
        }

        normalized = _normalize_pending_confirmations(payload)
        pending = [
            item
            for shot in normalized["shots"]
            for item in shot["pending_confirmations"]
        ]

        self.assertEqual(pending, [f"关键事实{index}" for index in range(1, 7)])

    @staticmethod
    def _plan():
        source_ref = {
            "source_file": "已审核模板.xlsx",
            "source_sheet": "模版",
            "source_row": 7,
        }
        return SimpleNamespace(
            brief={"项目名称": "测试发布会", "品牌或客户": "测试品牌"},
            constraints={},
            script_rows=[{
                "镜号": "1",
                "结构": "开场",
                "时长(秒)": 3,
                "效果": "字体穿梭",
                "类型": "航拍",
                "景别": "全景",
                "运镜": "缓慢推进",
                "画面描述": "已审核参考画面",
                "声音": "音乐起",
            }],
            detail_rows=[{
                "槽位功能": "品牌开场识别",
                "槽位选择原因": "先建立品牌视觉锚点",
                "允许素材": "品牌logo、主视觉KV",
                "禁止素材": "主持人、普通外场空镜",
                "推荐景别": "特写、全景",
                "推荐运镜": "固定、缓推",
                "推荐声音": "音乐起、科技脉冲",
                "节奏说明": "快速建立品牌识别",
                "必须视觉证据": "品牌logo",
                "重复素材规则": "片头只出现一次",
                "原始画面描述": "已审核参考画面",
                "采用来源证据": "品牌logo穿梭入画",
            }],
            source_refs=[source_ref],
        )

    @staticmethod
    def _response(description):
        return {
            "schema_version": SCHEMA_VERSION,
            "overall_summary": "围绕测试项目构建清晰的发布会叙事。",
            "shots": [{
                "shot_no": "1",
                "visual_description": description,
                "sound": "音乐起",
                "voiceover": "",
                "subtitle": "",
                "pending_confirmations": [],
            }],
        }


if __name__ == "__main__":
    unittest.main()
