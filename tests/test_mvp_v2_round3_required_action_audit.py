import unittest

from mvp_v2.audit import _required_term_present, _row_text, _required_term_present_in_row


class RequiredActionAuditTests(unittest.TestCase):
    def test_short_actions_keep_the_same_coverage_as_planning(self):
        for requirement, expression in (
            ("备米入锅", "备米放入内锅，动作自然"),
            ("备米入锅", "双手将备好的米倒入内锅，米粒落入锅底，随后内锅被平稳放入机身内"),
            ("备米入锅", "双手将米倒入内锅，米粒落底，随后内锅被平稳放入机身"),
            ("备米入锅", "把大米缓缓倒入内锅"),
            ("开盖成饭", "成饭开盖，内锅呈现成饭状态"),
            ("开盖成饭", "电饭煲开盖，呈现成饭状态"),
            ("开盖成饭", "电饭煲上盖缓缓开启，热气升起，锅内米饭成形饱满"),
            ("开盖成饭", "双手揭开锅盖，煮熟的米饭热气升腾"),
            ("开盖成饭", "上盖已打开，内锅盛着煮好的米饭"),
            ("开盖成饭", "上盖缓缓开启，热气升起，锅内米饭饱满成型"),
        ):
            with self.subTest(requirement=requirement, expression=expression):
                self.assertTrue(_required_term_present(requirement, expression))

    def test_a_single_action_or_subject_is_insufficient(self):
        for expression in ("电饭煲外观", "备米", "放入内锅", "打开上盖"):
            with self.subTest(expression=expression):
                self.assertFalse(_required_term_present("备米入锅", expression))

    def test_excluded_reordered_actions_do_not_cover_the_requirement(self):
        for expression in ("禁止备米放入内锅", "未成饭开盖", "未开盖，未成饭"):
            with self.subTest(expression=expression):
                self.assertFalse(_required_term_present("备米入锅" if "备米" in expression else "开盖成饭", expression))

    def test_model_identity_is_not_relaxed_to_action_matching(self):
        self.assertFalse(_required_term_present("MB-AB1234", "MB-AB4321产品外观"))

    def test_appearance_can_be_grounded_in_visible_body_without_the_abstract_label(self):
        self.assertTrue(_required_term_present('外观', '银色机身与上盖清晰，镜头缓推产品'))
        for text in ('热气升起', '不展示机身', '禁止呈现产品外观', '避免出现外壳'):
            self.assertFalse(_required_term_present('外观', text), text)

    def test_postproduction_caption_is_audited_as_output_content(self):
        row = {"画面描述": "电饭煲外观与操作面板", "字幕": "匿名 MB-AB1234 4L IH加热"}
        self.assertTrue(_required_term_present("IH加热字幕", _row_text(row)))
        self.assertFalse(_required_term_present("IH加热字幕", _row_text({**row, "字幕": "匿名 MB-AB1234"})))
        self.assertFalse(_required_term_present("IH加热字幕", "不出现IH加热字幕"))
        self.assertTrue(_required_term_present_in_row("IH加热字幕", row))
        self.assertTrue(_required_term_present_in_row("IH加热字幕", {"画面描述": "字幕呈现IH加热"}))
        self.assertFalse(_required_term_present_in_row("IH加热字幕", {"声音": "旁白介绍IH加热"}))

    def test_paraphrased_action_keeps_negation_and_complete_action(self):
        for text in ("备好的米留在碗里，内锅被放入机身", "备好的米禁止倒入内锅", "不将备好的米倒入内锅", "禁止将米倒入内锅", "将米倒入碗中，空内锅留在旁边"):
            self.assertFalse(_required_term_present("备米入锅", text), text)
        for text in ("上盖开启，内锅空置", "熟米饭已上桌，锅盖关闭", "禁止打开锅盖，米饭仍未煮熟", "上盖开启，米饭仍未成型"):
            self.assertFalse(_required_term_present("开盖成饭", text), text)


if __name__ == "__main__":
    unittest.main()
