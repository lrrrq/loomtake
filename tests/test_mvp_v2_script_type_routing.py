import tempfile
import unittest
from pathlib import Path

from mvp_v2.audit import (
    _brief_supports_required_term,
    _required_term_present,
    _source_supports_required_term,
    _source_specific_latin_terms,
    audit_generation,
)
from mvp_v2.excel_io import BriefError, read_text_brief
from mvp_v2.generator import generate_template_rows
from mvp_v2.image_assets import build_supplemental_image_asset
from mvp_v2.models import (
    SourceShot,
    TemplateDecision,
    split_forbidden_terms,
    validate_script_brief,
)
from mvp_v2.scoring import match_template_slots
from mvp_v2.script_type_adapter import (
    SCRIPT_TYPE_METADATA,
    category_id_for_script_type,
    normalize_script_type,
    resolve_script_type_selection,
    resolve_script_type_workbook,
    route_contract_for_script_type,
)


class ScriptTypeRoutingTests(unittest.TestCase):
    def test_supplemental_image_keeps_non_launch_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "candidate.png"
            from PIL import Image
            Image.new("RGB", (1280, 720), (20, 40, 60)).save(image)

            row = build_supplemental_image_asset(image, "潮汐声场", "音乐节")

        self.assertEqual(row["活动类型"], "音乐节")
        self.assertEqual(row["项目名"], "潮汐声场")

    def test_natural_language_forbidden_terms_are_atomic(self):
        self.assertEqual(
            split_forbidden_terms("不要烟花、热气球和模特走秀；不虚构艺人阵容或赞助商"),
            ["烟花", "热气球", "模特走秀", "艺人阵容", "赞助商"],
        )

    def test_non_launch_audit_blocks_natural_language_forbidden_content(self):
        slot = SourceShot(
            script_type="年会", source_file="annual.xlsx", source_sheet="模版",
            source_row=7, shot_no="1", structure="氛围造境", duration_seconds="5",
            visual_desc="热气球缓缓升起", shot_function="大型装置互动",
            visual_subject="热气球装置", slot_index=1,
        )
        decision = TemplateDecision(
            slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板",
        )
        rows = [{"结构": "氛围造境", "镜号": "1", "时长(秒)": 5, "画面描述": "热气球缓缓升起"}]
        details = [{
            "slot_index": 1, "模板镜号": "1", "槽位功能": "大型装置互动",
            "采用来源文件": "annual.xlsx", "采用来源 sheet": "模版", "采用来源行": 7,
        }]

        result = audit_generation(
            {
                "项目名称": "室内年会", "品牌或客户": "测试品牌", "脚本类型": "年会",
                "视频时长": "5s", "禁忌或不能出现的内容": "不要烟花、热气球和模特走秀",
            },
            [slot], [decision], rows, details,
        )

        self.assertIn("brief_forbidden_hit", result["blocking_items"])

    def test_registry_has_six_exact_routes(self):
        self.assertEqual(
            [item.display_name for item in SCRIPT_TYPE_METADATA],
            ["发布会", "展会", "年会", "赛事", "音乐节", "明星活动"],
        )
        self.assertEqual(category_id_for_script_type("明星活动"), "star_activity")
        self.assertEqual(normalize_script_type("music-festival"), "音乐节")
        with self.assertRaisesRegex(ValueError, "不支持的脚本类型"):
            normalize_script_type("非发布会")

    def test_descriptive_specific_type_has_one_safe_preflight_route(self):
        result = resolve_script_type_selection("活动短视频", "发布会活动回顾视频")

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["script_type"], "发布会")
        self.assertEqual(result["category_id"], "launch")
        self.assertEqual(result["authority"], "industry_or_activity_category")
        self.assertEqual(
            resolve_script_type_selection("非发布会", "")["status"],
            "unsupported",
        )
        valentine = resolve_script_type_selection(
            "活动短视频", "七夕快闪互动回顾片",
        )
        self.assertEqual(valentine["status"], "ready")
        self.assertEqual(valentine["category_id"], "star_activity")

    def test_preflight_keeps_supported_raw_type_and_reports_real_conflict(self):
        self.assertEqual(
            resolve_script_type_selection("明星活动", "七夕快闪互动回顾片")["script_type"],
            "明星活动",
        )
        conflict = resolve_script_type_selection("发布会", "明星活动应援回顾")
        self.assertEqual(conflict["status"], "needs_clarification")

    def test_route_contract_freezes_capability_boundaries(self):
        launch = route_contract_for_script_type("发布会")
        exhibition = route_contract_for_script_type("展会")

        self.assertEqual(launch["duration_policy"], "fixed_60_seconds")
        self.assertEqual(launch["planning_profile"], "launch_slot_contract")
        self.assertEqual(exhibition["planning_profile"], "ai_spec_function_contract")
        self.assertEqual(exhibition["render_profile"], "source_workbook_template")
        self.assertFalse(exhibition["cross_category_sources"])
        self.assertIn("AI_规范表", exhibition["source_sheets"])

    def test_resolver_selects_each_category_ai_workbook(self):
        names = {
            "发布会": "M+发布会类通用脚本库 AI版v1.xlsx",
            "展会": "M+展会类通用脚本库_AI版v1.xlsx",
            "年会": "M+年会类通用脚本库_AI版v1.xlsx",
            "赛事": "M+赛事类通用脚本库_AI版v1.xlsx",
            "音乐节": "M+音乐节通用脚本库_AI版v1.xlsx",
            "明星活动": "M+明星活动类通用脚本库_AI版v1.xlsx",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in names.values():
                (root / name).write_bytes(b"fixture")
            (root / "~$M+展会类通用脚本库_AI版v2.xlsx").write_bytes(b"lock")

            for script_type, name in names.items():
                with self.subTest(script_type=script_type):
                    self.assertEqual(resolve_script_type_workbook(root, script_type).name, name)

    def test_single_workbook_cannot_be_reused_as_another_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            launch = Path(tmp) / "M+发布会类通用脚本库 AI版v1.xlsx"
            launch.write_bytes(b"fixture")

            self.assertEqual(resolve_script_type_workbook(launch, "发布会"), launch)
            self.assertNotEqual(resolve_script_type_workbook(launch, "展会"), launch)

    def test_duration_policy_is_routed_by_type(self):
        self.assertEqual(validate_script_brief({"脚本类型": "发布会", "视频时长": "60s"}), 60)
        self.assertEqual(validate_script_brief({"脚本类型": "发布会", "视频时长": "90s"}), 90)
        self.assertEqual(validate_script_brief({"脚本类型": "展会", "视频时长": "90s"}), 90)

    def test_generic_required_content_accepts_clear_equivalent_visual_wording(self):
        self.assertTrue(_brief_supports_required_term(
            "匿名必须项",
            {
                "必须出现的内容": "匿名必须项",
                "_formal_fact_evidence": {
                    "必须出现的内容": {
                        "fact_id": "fact-anonymous",
                        "evidence_refs": ["artifact://formal-evidence/anonymous/paragraph/1"],
                    },
                },
            },
            {},
        ))
        self.assertTrue(_brief_supports_required_term(
            "匿名必须项",
            {
                "必须出现的内容": "匿名必须项",
                "_formal_fact_evidence": {
                    "必须出现的内容": {
                        "fact_id": "fact-anonymous",
                        "evidence_refs": [{
                            "artifact_ref": "artifact://formal-evidence/anonymous/paragraph/1",
                            "content_sha256": "a" * 64,
                            "source_document_id": "source-anonymous",
                        }],
                    },
                },
            },
            {},
        ))
        self.assertTrue(_required_term_present("品牌展台", "镜头缓推衡岚曲面展台"))
        self.assertTrue(_required_term_present("核心产品", "窄轮廓光勾勒产品外形与关键结构"))
        self.assertTrue(_required_term_present("互动体验", "嘉宾上手互动并分享体验"))
        self.assertTrue(_required_term_present("品牌落版", "衡岚 LOGO 渐显完成落版"))
        self.assertTrue(_required_term_present("员工签到", "员工在签到墙用马克笔签名"))
        self.assertTrue(_required_term_present("团队颁奖", "获奖团队走上舞台接受颁奖"))
        self.assertTrue(_required_term_present("团队颁奖", "优秀团队列队上台接受颁奖"))
        self.assertTrue(_required_term_present("集体合影", "高管与看台全体员工完成超大规模合影"))
        self.assertTrue(_required_term_present("举办城市与活动信息", "航拍城市主干道与地标，展现活动氛围"))
        self.assertTrue(_required_term_present("发令启动", "特写举起发令装置的手部，定格赛事启动瞬间"))
        self.assertTrue(_required_term_present("起跑集结", "航拍起跑门与密集跑者方阵"))
        self.assertTrue(_required_term_present("赛事标识", "逐峰城市挑战赛主视觉与品牌标识居中定格"))
        self.assertTrue(_required_term_present("竞技瞬间", "手持跟拍参赛者冲出起点"))
        self.assertTrue(_required_term_present("赛事主题落版", "逐峰城市挑战赛主视觉居中定格"))
        self.assertTrue(_required_term_present("城市与活动信息", "动态标题展示音乐节名称及日期、城市信息"))
        self.assertTrue(_required_term_present("歌手演出", "低角度仰拍歌手在圆形屏下表演"))
        self.assertTrue(_required_term_present("观众互动", "观众在彩色织物装置间穿行、触摸、合影"))
        self.assertTrue(_required_term_present("活动品牌落版", "黑场渐显潮汐声场品牌标识，短暂停留后淡出"))
        self.assertTrue(_required_term_present("会场与品牌", "澜序活动场地作为落点亮灯，品牌标识清晰可见"))
        self.assertTrue(_required_term_present("艺人亮相", "艺人逆光背影转身，切至正面微笑亮相"))
        self.assertTrue(_required_term_present("艺人核心表达", "艺人在主舞台中央向全场表达核心感言"))
        self.assertTrue(_required_term_present("品牌落版", "航拍拉远，LOGO于黑场淡入"))
        self.assertTrue(_required_term_present("荣誉环节", "优秀团队列队上台领奖"))
        self.assertTrue(_required_term_present("员工入场", "第一视角跟随员工穿过入口通道"))
        self.assertTrue(_required_term_present("高管致辞", "高管近景致辞，分屏并置舞台全景"))
        self.assertTrue(_required_term_present("品牌落版", "企业Logo与年度主题KV居中浮现，完成品牌收官"))
        self.assertTrue(_required_term_present("品牌落版", "栖光软件Logo与年度盛典字样居中浮现，音乐延音渐止"))
        self.assertTrue(_required_term_present("主视觉", "音乐节主舞台与品牌标识浮现"))
        self.assertTrue(_required_term_present("明星出场", "艺人逆光转身并正面亮相"))
        self.assertTrue(_required_term_present("产品互动", "艺人上手试香并与产品互动"))
        self.assertTrue(_required_term_present("冲线或颁奖", "跑者冲线后举起奖牌"))
        self.assertTrue(_required_term_present("战队入场", "战队选手穿过入口步入内场"))
        self.assertTrue(_required_term_present("邀请函入场", "第一视角手持邀请函对准入口后继续前行"))
        self.assertTrue(_required_term_present("设备准备", "成排电竞主机与外设调试就位"))
        self.assertTrue(_required_term_present("比赛对抗", "选手在主舞台赛场开始竞技对抗"))
        self.assertTrue(_required_term_present("比赛对抗", "俯拍主舞台对战区，选手键鼠操作快切"))
        self.assertTrue(_required_term_present("观众应援", "粉丝高举应援棒与灯牌，齐声呐喊"))
        self.assertTrue(_required_term_present("颁奖合影", "战队在颁奖区集体合影定格"))
        self.assertTrue(_required_term_present(
            "邀请函第一视角入场",
            "第一视角双手举起邀请函，对准入口后继续前行并步入赛场",
        ))
        self.assertTrue(_required_term_present(
            "网球运动员挥拍击球",
            "一名女子在网球场发球，随后挥拍击球",
        ))
        self.assertTrue(_required_term_present(
            "赛事集体合影",
            "参赛者在主题区完成集体合影并定格",
        ))
        self.assertTrue(_required_term_present(
            "捕捉情侣撤开隔板后的甜蜜小表情/动作",
            "答案一致时隔板撤开，捕捉情侣甜蜜表情与互动",
        ))
        self.assertTrue(_required_term_present(
            "到门店开礼盒领取小礼物（突出门店门头）",
            "情侣走向MONOLOGUE门店，门头清晰可见。进入店内打开礼盒，取出小礼物",
        ))
        self.assertTrue(_required_term_present(
            "开头需要ai蝴蝶从kv中欧式花园飞出",
            "蝴蝶从KV欧式花园飞出",
        ))
        self.assertTrue(_required_term_present(
            "落到现场布置空镜中做转场",
            "镜头跟随蝴蝶飞向现场布置空镜",
        ))
        self.assertTrue(_required_term_present(
            "现场外场空镜展示",
            "航拍推进会展中心外场，入口人流退去后完成外场空镜展示",
        ))
        self.assertTrue(_required_term_present(
            "内场空镜展示",
            "内场空镜，舞台灯光璀璨，大屏显示大会主题",
        ))
        self.assertTrue(_required_term_present(
            "国内代表1发言(全景+中景)",
            "国内代表1发言全景，随后切中景，背景大屏显示全球布局",
        ))
        self.assertTrue(_required_term_present(
            "国外代表6发言(全景+中景)",
            "国外代表6发言全景，随后中景，代表总结合作愿景",
        ))
        self.assertTrue(_required_term_present(
            "机器狗展示",
            "国外代表发言后，机器狗在舞台一侧展示",
        ))
        self.assertFalse(_required_term_present(
            "机器狗展示",
            "现场没有机器狗展示",
        ))
        self.assertTrue(_required_term_present(
            "轮胎互动挑战", "俯拍赛道装置，再切轮胎挑战互动的手部与器材细节"
        ))
        self.assertTrue(_required_term_present(
            "主题入口", "赛事主题区以星形拱门作为入口装置"
        ))
        self.assertTrue(_required_term_present(
            "主题入口", "无畏契约赛事入口装置前聚集现场观众"
        ))
        self.assertFalse(_required_term_present(
            "轮胎互动挑战", "赛道旁整齐陈列轮胎，没有人员操作"
        ))
        self.assertFalse(_required_term_present(
            "主题入口", "普通观众从通道入场，没有主题标识"
        ))
        self.assertFalse(_required_term_present("颁奖合影", "选手登台领奖，观众欢呼"))
        self.assertFalse(_required_term_present("团队颁奖", "员工在舞台前集体合影"))
        self.assertFalse(_required_term_present("集体合影", "员工在舞台前齐声欢呼"))
        self.assertFalse(_required_term_present(
            "网球运动员挥拍击球",
            "普通运动员在舞台上挥手",
        ))

    def test_confirmed_formal_required_content_is_written_and_auditable(self):
        slot = SourceShot(
            script_type="明星活动", source_file="star.xlsx", source_sheet="模版",
            source_row=8, shot_no="1", structure="空间巡游", duration_seconds="5",
            visual_desc="情侣挑战现场中景", shot_function="现场空间建立",
            visual_subject="活动现场", slot_index=1,
        )
        decision = TemplateDecision(
            slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板",
        )
        brief = {
            "项目名称": "匿名明星活动", "品牌或客户": "匿名品牌", "脚本类型": "明星活动",
            "行业或活动类别": "快闪互动", "视频时长": "5s",
            "必须出现的内容": "落到现场布置空镜中做转场",
            "_formal_fact_evidence": {
                "必须出现的内容": {
                    "fact_id": "fact-required",
                    "evidence_refs": [{
                        "artifact_ref": "artifact://formal-evidence/source/paragraph/1",
                        "content_sha256": "b" * 64,
                        "source_document_id": "source-document",
                    }],
                },
            },
        }

        rows, details = generate_template_rows(brief, [decision])
        result = audit_generation(brief, [slot], [decision], rows, details)

        self.assertTrue(_required_term_present(brief["必须出现的内容"], rows[0]["画面描述"]))
        self.assertNotIn("required_content_missing", result["blocking_items"])
        self.assertNotIn("required_content_evidence_missing", result["blocking_items"])
        self.assertFalse(_required_term_present("发令启动", "主舞台灯光启动"))
        self.assertFalse(_required_term_present("产品互动", "产品展示台空镜"))
        self.assertFalse(_source_supports_required_term("产品互动", "参与者触发拳击互动装置"))
        self.assertTrue(_source_supports_required_term("产品互动", "艺人拿起产品试用并与主持人互动"))

    def test_music_festival_project_wording_keeps_functional_source_evidence(self):
        self.assertTrue(
            _required_term_present("城市公园场地", "航拍城市公园全貌，人群沿草坪入场")
        )
        self.assertTrue(
            _required_term_present("观众灯光互动", "灯光装置前观众举起手机互动打卡")
        )
        self.assertFalse(
            _required_term_present("观众灯光互动", "观众在草坪上合影打卡")
        )
        self.assertTrue(
            _required_term_present("电子舞台表演", "平移串联舞台歌手、乐队与观众回应")
        )

        self.assertTrue(
            _source_supports_required_term("城市公园场地", "音乐节周边草原与目的地场地远景")
        )
        self.assertTrue(
            _source_supports_required_term("观众灯光互动", "观众穿行装置，捕捉触摸、合影体验")
        )
        self.assertTrue(
            _source_supports_required_term("电子舞台表演", "平移串联舞台歌手、乐队与观众")
        )

    def test_non_launch_generator_does_not_forge_required_content_evidence(self):
        slot = SourceShot(
            script_type="明星活动", source_file="star.xlsx", source_sheet="模版",
            source_row=7, shot_no="1", structure="开场", duration_seconds="5",
            visual_desc="傍晚城市建筑与飞鸟", shot_function="城市氛围开场",
            visual_subject="城市建筑", slot_index=1,
        )
        decision = TemplateDecision(
            slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板",
        )

        rows, details = generate_template_rows(
            {
                "项目名称": "青屿见面日", "品牌或客户": "青屿", "脚本类型": "明星活动",
                "视频时长": "5s", "必须出现的内容": "明星出场",
            },
            [decision],
        )

        self.assertNotIn("明星出场", rows[0]["画面描述"])
        self.assertNotIn("镜头明确呈现", rows[0]["画面描述"])
        self.assertIn("须由已审核来源文字或已识别参考图提供证据", details[0]["缺口说明"])

    def test_non_launch_generator_removes_source_case_facts_from_customer_copy(self):
        slot = SourceShot(
            script_type="展会", source_file="expo.xlsx", source_sheet="模版",
            source_row=7, shot_no="1", structure="开场", duration_seconds="5",
            visual_desc="航拍上海展馆，穿过VIGOUR CUBE后切机器人互动",
            shot_function="举办地与展会信息建立", visual_subject="城市、展馆、展项",
            source_brand="来源品牌", slot_index=1,
        )
        decision = TemplateDecision(
            slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板",
        )
        brief = {
            "项目名称": "衡岚循环材料未来展", "品牌或客户": "衡岚", "脚本类型": "展会",
            "行业或活动类别": "循环材料与低碳科技展会", "视频时长": "5s",
        }

        rows, details = generate_template_rows(brief, [decision])
        result = audit_generation(brief, [slot], [decision], rows, details)

        visible = rows[0]["画面描述"]
        self.assertNotIn("上海", visible)
        self.assertNotIn("VIGOUR", visible)
        self.assertNotIn("CUBE", visible)
        self.assertNotIn("机器人", visible)
        self.assertIn("衡岚循环材料未来展", visible)
        self.assertNotIn("source_specific_term_leak", result["blocking_items"])
        self.assertNotIn("unconfirmed_location_fact", result["blocking_items"])
        self.assertNotIn("unsupported_source_subject", result["blocking_items"])

    def test_non_launch_generator_writes_required_content_only_with_approved_source_basis(self):
        slots = [
            SourceShot(
                script_type="赛事", source_file="sports.xlsx", source_sheet="模版",
                source_row=7, shot_no="1", structure="竞技瞬间", duration_seconds="5",
                visual_desc="航拍起跑门与密集跑者方阵，完整交代赛道规模",
                shot_function="起跑集结规模建立", visual_subject="跑者、起跑区、赛道",
                source_brand="来源赛事", slot_index=1,
            ),
            SourceShot(
                script_type="赛事", source_file="sports.xlsx", source_sheet="模版",
                source_row=8, shot_no="2", structure="品牌落版", duration_seconds="5",
                visual_desc="记录颁奖与场馆高潮，观众为参赛者欢呼",
                shot_function="颁奖与场馆高潮", visual_subject="参赛者、观众、场馆",
                source_brand="来源赛事", slot_index=2,
            ),
        ]
        decisions = [
            TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板")
            for slot in slots
        ]
        brief = {
            "项目名称": "逐峰城市挑战赛", "品牌或客户": "逐峰运动", "脚本类型": "赛事",
            "行业或活动类别": "城市综合挑战赛", "视频时长": "10s",
            "必须出现的内容": "起跑集结、冲线或颁奖",
            "备注": "具体成绩与选手信息标记待确认",
        }

        rows, details = generate_template_rows(brief, decisions)
        result = audit_generation(brief, slots, decisions, rows, details)

        self.assertTrue(_required_term_present("起跑集结", rows[0]["画面描述"]))
        self.assertTrue(_required_term_present("冲线或颁奖", rows[1]["画面描述"]))
        self.assertNotIn("required_content_missing", result["blocking_items"])
        self.assertNotIn("required_content_evidence_missing", result["blocking_items"])

    def test_non_launch_generator_rewrites_mismatched_source_event_title(self):
        slot = SourceShot(
            script_type="音乐节", source_file="music.xlsx", source_sheet="模版",
            source_row=7, shot_no="1", structure="夜幕狂欢", duration_seconds="5",
            visual_desc="FPV穿越318生活节舞台和烟火阵列，呈现多层烟火轨迹",
            shot_function="烟火秀穿越机高光", visual_subject="舞台、观众、烟火",
            source_brand="广州风暴电音节", slot_index=1,
        )
        decision = TemplateDecision(
            slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板",
        )
        brief = {
            "项目名称": "潮汐声场海岸音乐节", "品牌或客户": "潮汐声场",
            "脚本类型": "音乐节", "行业或活动类别": "户外音乐节", "视频时长": "5s",
        }

        rows, _ = generate_template_rows(brief, [decision])

        self.assertNotIn("318生活节", rows[0]["画面描述"])
        self.assertNotIn("广州风暴", rows[0]["画面描述"])
        self.assertIn("潮汐声场海岸音乐节", rows[0]["画面描述"])

    def test_unrelated_embedded_reference_does_not_prove_required_content(self):
        slot = SourceShot(
            script_type="明星活动", source_file="star.xlsx", source_sheet="模版",
            source_row=7, shot_no="1", structure="开场", duration_seconds="5",
            visual_desc="傍晚城市建筑与飞鸟", shot_function="城市氛围开场",
            visual_subject="城市建筑", reference_image_path="/tmp/city.png",
            reference_image_evidence="源表嵌图", slot_index=1,
        )
        decision = TemplateDecision(
            slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板",
        )
        rows = [{
            "结构": "开场", "镜号": "1", "时长(秒)": 5,
            "画面描述": "镜头明确呈现明星出场",
        }]
        details = [{
            "slot_index": 1, "模板镜号": "1", "槽位功能": "城市氛围开场",
            "采用来源文件": "star.xlsx", "采用来源 sheet": "模版", "采用来源行": 7,
        }]

        result = audit_generation(
            {
                "项目名称": "青屿见面日", "品牌或客户": "青屿", "脚本类型": "明星活动",
                "视频时长": "5s", "必须出现的内容": "明星出场",
            },
            [slot], [decision], rows, details,
        )

        self.assertIn("required_content_evidence_missing", result["blocking_items"])

    def test_project_specific_relay_can_use_approved_generic_competition_basis(self):
        slot = SourceShot(
            script_type="赛事", source_file="sports.xlsx", source_sheet="模版",
            source_row=18, shot_no="1", structure="竞技高潮", duration_seconds="5",
            visual_desc="运动员在赛道完成高强度竞技挑战", shot_function="赛事竞技过程",
            visual_subject="运动员、赛道、观众", slot_index=1,
        )
        decision = TemplateDecision(
            slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板",
        )
        rows = [{
            "结构": "竞技高潮", "镜号": "1", "时长(秒)": 5,
            "画面描述": "接力跑者在夜间赛道完成交接棒，跟拍进入竞技高潮",
        }]
        details = [{
            "slot_index": 1, "模板镜号": "1", "槽位功能": "赛事竞技过程",
            "采用来源文件": "sports.xlsx", "采用来源 sheet": "模版", "采用来源行": 18,
        }]

        result = audit_generation(
            {
                "项目名称": "城市接力赛", "品牌或客户": "跑团", "脚本类型": "赛事",
                "视频时长": "5s", "必须出现的内容": "接力竞技",
            },
            [slot], [decision], rows, details,
        )

        self.assertNotIn("required_content_evidence_missing", result["blocking_items"])

    def test_non_launch_audit_blocks_unconfirmed_source_location_and_subject(self):
        slot = SourceShot(
            script_type="展会", source_file="expo.xlsx", source_sheet="模版",
            source_row=18, shot_no="1", structure="收束", duration_seconds="5",
            visual_desc="航拍上海天际线，机器人在展台互动", shot_function="城市与展馆收束",
            visual_subject="城市、展馆、展台", slot_index=1,
        )
        decision = TemplateDecision(
            slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板",
        )
        rows = [{
            "结构": "收束", "镜号": "1", "时长(秒)": 5,
            "画面描述": "航拍上海城市天际线，切机器人舞蹈互动",
        }]
        details = [{
            "slot_index": 1, "模板镜号": "1", "槽位功能": "城市与展馆收束",
            "采用来源文件": "expo.xlsx", "采用来源 sheet": "模版", "采用来源行": 18,
            "采用来源证据": slot.searchable_text(),
        }]

        result = audit_generation(
            {
                "项目名称": "循环材料未来展", "品牌或客户": "衡岚", "脚本类型": "展会",
                "视频时长": "5s", "必须出现的内容": "",
                "行业或活动类别": "循环材料与低碳科技展会",
            },
            [slot], [decision], rows, details,
        )

        self.assertIn("unconfirmed_location_fact", result["blocking_items"])
        self.assertIn("unsupported_source_subject", result["blocking_items"])

    def test_source_specific_terms_are_detected_without_static_brand_names(self):
        brief = {"品牌或客户": "沐野智造", "必须出现的内容": "品牌LOGO"}
        evidence = "第一视角穿过ZEISS蓝色VIGOUR CUBE互动通道"

        self.assertEqual(
            _source_specific_latin_terms(
                "沐野智造VIGOUR CUBE互动通道，LOGO随镜头出现",
                evidence,
                brief,
            ),
            ["VIGOUR", "CUBE"],
        )
        self.assertEqual(
            _source_specific_latin_terms("FPV穿越夜场烟火", "FPV飞行镜头", brief),
            [],
        )
        self.assertEqual(
            _source_specific_latin_terms("KOL自拍记录现场", "KOL与嘉宾到场", brief),
            [],
        )

    def test_non_launch_model_contract_receives_source_forbidden_note(self):
        slot = SourceShot(
            script_type="年会", source_file="annual.xlsx", source_sheet="模版",
            source_row=7, shot_no="1", structure="氛围造境", duration_seconds="5",
            visual_desc="航拍来源品牌年会现场", shot_function="户外会场规模建立",
            source_brand="来源品牌", forbidden_note="不得直接照搬来源品牌专属年会物料",
            slot_index=1,
        )
        decision = TemplateDecision(
            slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板",
        )

        _, details = generate_template_rows(
            {"项目名称": "沐野年会", "品牌或客户": "沐野智造", "脚本类型": "年会", "视频时长": "5s"},
            [decision],
        )

        self.assertEqual(details[0]["禁止素材"], slot.forbidden_note)

    def test_non_launch_creative_must_match_the_ai_spec_shot_function(self):
        slot = SourceShot(
            script_type="年会", source_file="annual.xlsx", source_sheet="模版",
            source_row=7, shot_no="1", structure="颁奖典礼", duration_seconds="5",
            visual_desc="高管在主舞台致辞", shot_function="高管分屏致辞",
            visual_subject="高管、舞台、观众", slot_index=1,
        )
        wrong = SourceShot(
            script_type="年会", source_file="annual.xlsx", source_sheet="创意库",
            source_row=20, shot_no="1", structure="颁奖典礼", duration_seconds="5",
            visual_desc="低机位跟随模特穿过舞台", shot_function="模特走秀",
            visual_subject="员工、舞台、观众", source_brand="来源品牌",
            reference_image_path="annual.xlsx#创意库!R20C4", slot_index=1,
        )
        exact = SourceShot(
            script_type="年会", source_file="annual.xlsx", source_sheet="创意库",
            source_row=21, shot_no="1", structure="颁奖典礼", duration_seconds="5",
            visual_desc="高管致辞与观众反应分屏呈现", shot_function="高管分屏致辞",
            visual_subject="高管、舞台、观众", source_brand="来源品牌",
            reference_image_path="annual.xlsx#创意库!R21C4", slot_index=1,
        )
        brief = {
            "项目名称": "沐野智造年会", "品牌或客户": "沐野智造", "脚本类型": "年会",
            "视频时长": "5s", "风格调性": "温暖", "必须出现的内容": "高管致辞",
            "禁忌或不能出现的内容": "",
        }

        rejected = match_template_slots(brief, [slot], [wrong])[0]
        adopted = match_template_slots(brief, [slot], [wrong, exact])[0]

        self.assertEqual(rejected.mode, "KEEP")
        self.assertIsNone(rejected.variant)
        self.assertIn(adopted.mode, {"REPLACE", "ADAPT"})
        self.assertEqual(adopted.variant.source_row, 21)

    def test_non_launch_audit_blocks_one_description_for_distinct_functions(self):
        slots = [
            SourceShot(
                script_type="年会", source_file="annual.xlsx", source_sheet="模版",
                source_row=7, shot_no="1", structure="颁奖典礼", duration_seconds="5",
                visual_desc="高管致辞", shot_function="高管分屏致辞", slot_index=1,
            ),
            SourceShot(
                script_type="年会", source_file="annual.xlsx", source_sheet="模版",
                source_row=8, shot_no="2", structure="颁奖典礼", duration_seconds="5",
                visual_desc="员工应援", shot_function="员工灯海应援", slot_index=2,
            ),
        ]
        decisions = [
            TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0, match_reason="保留模板")
            for slot in slots
        ]
        rows = [
            {"结构": "颁奖典礼", "镜号": str(i), "画面描述": "模特在舞台上行走", "时长(秒)": 5}
            for i in (1, 2)
        ]
        details = [
            {
                "slot_index": i, "模板镜号": str(i), "槽位功能": slot.shot_function,
                "采用来源文件": "annual.xlsx", "采用来源 sheet": "模版", "采用来源行": slot.source_row,
            }
            for i, slot in enumerate(slots, start=1)
        ]
        result = audit_generation(
            {"项目名称": "年会", "品牌或客户": "沐野", "脚本类型": "年会", "视频时长": "10s"},
            slots, decisions, rows, details,
        )

        self.assertIn("duplicate_distinct_function_description", result["blocking_items"])

    def test_star_activity_routes_missing_product_interaction_to_approved_creative(self):
        slots = [
            SourceShot(
                script_type="明星活动", source_file="star.xlsx", source_sheet="模版",
                source_row=17, shot_no="11", structure="艺人亮相", slot_index=11,
                shot_function="艺人到场亮相", visual_desc="艺人转身正面亮相",
                visual_subject="艺人、主题墙",
            ),
            SourceShot(
                script_type="明星活动", source_file="star.xlsx", source_sheet="模版",
                source_row=19, shot_no="13", structure="艺人亮相", slot_index=13,
                shot_function="艺人核心表达", visual_desc="艺人在主舞台致辞",
                visual_subject="艺人、舞台、观众",
            ),
        ]
        product_interaction = SourceShot(
            script_type="明星活动", source_file="star.xlsx", source_sheet="创意库",
            source_row=45, shot_no="39", structure="艺人亮相", slot_index=0,
            shot_function="直播互动与产品种草",
            visual_desc="艺人与主持人在直播间围绕产品互动",
            visual_subject="艺人、产品、直播间",
            source_brand="VOGUE",
            reference_image_path="star.xlsx#创意库!R45C4",
        )
        decisions = match_template_slots({
            "项目名称": "青屿香氛品牌见面日",
            "脚本类型": "明星活动",
            "必须出现的内容": "明星出场、产品互动",
            "禁忌或不能出现的内容": "",
        }, slots, [product_interaction])

        self.assertEqual(decisions[0].mode, "KEEP")
        self.assertIsNone(decisions[0].variant)
        self.assertEqual(decisions[1].mode, "REPLACE")
        self.assertEqual(decisions[1].variant.source_row, 45)
        self.assertIn("Brief必选来源补位:产品互动", decisions[1].match_reason)

    def test_required_source_coverage_does_not_force_unrelated_creative(self):
        slot = SourceShot(
            script_type="明星活动", source_file="star.xlsx", source_sheet="模版",
            source_row=17, shot_no="11", structure="艺人亮相", slot_index=11,
            shot_function="艺人到场亮相", visual_desc="艺人转身正面亮相",
            visual_subject="艺人、主题墙",
        )
        unrelated = SourceShot(
            script_type="明星活动", source_file="star.xlsx", source_sheet="创意库",
            source_row=45, shot_no="39", structure="艺人亮相", slot_index=0,
            shot_function="直播互动与产品种草",
            visual_desc="艺人与主持人在直播间围绕产品互动",
            visual_subject="艺人、产品、直播间",
            source_brand="VOGUE",
        )

        decisions = match_template_slots({
            "项目名称": "青屿香氛品牌见面日",
            "脚本类型": "明星活动",
            "必须出现的内容": "火星基地实拍",
            "禁忌或不能出现的内容": "",
        }, [slot], [unrelated])

        self.assertEqual(decisions[0].mode, "KEEP")
        self.assertIsNone(decisions[0].variant)
        self.assertNotIn("Brief必选来源补位", decisions[0].match_reason)

    def test_ambiguous_freeform_type_requires_explicit_route(self):
        with self.assertRaisesRegex(BriefError, "同时出现多个脚本类型"):
            read_text_brief("""
项目名称：综合活动开场片
项目区域文件名：综合活动.xlsx
视频时长：90s
风格调性：现代、有节奏
必须出现的内容：品牌LOGO
备注：既提到展会也提到年会，但没有指定当前类型
""")

    def test_cross_category_source_is_a_hard_failure(self):
        slot = SourceShot(
            script_type="发布会",
            source_file="launch.xlsx",
            source_sheet="模版",
            source_row=7,
            shot_no="1",
            structure="开场",
            duration_seconds="30",
            visual_desc="品牌LOGO出现",
            shot_function="品牌开场",
            slot_index=1,
        )
        decision = TemplateDecision(
            slot=slot,
            variant=None,
            mode="KEEP",
            score=0,
            match_reason="fixture",
        )
        script_rows = [{
            "结构": "开场",
            "镜号": "1",
            "画面描述": "品牌LOGO出现",
            "时长(秒)": 30,
        }]
        detail_rows = [{
            "slot_index": 1,
            "模板镜号": "1",
            "槽位功能": "品牌开场",
            "采用来源文件": "launch.xlsx",
            "采用来源 sheet": "模版",
            "采用来源行": 7,
        }]

        result = audit_generation(
            {"脚本类型": "展会", "视频时长": "30s"},
            [slot],
            [decision],
            script_rows,
            detail_rows,
        )

        self.assertIn("cross_category_source_forbidden", result["blocking_items"])


if __name__ == "__main__":
    unittest.main()
