import unittest

from mvp_v2.image_retrieval import search_images_for_slot


class CurrentShotEvidenceTests(unittest.TestCase):
    def test_specific_subjects_and_actions_need_visual_evidence_but_remain_browsable(self):
        cases = [
            ('音乐节', '吉他手演奏吉他', '观众拿手机拍照', '吉他手在舞台上演奏吉他'),
            ('运动类', '跑者在户外奔跑比赛', '运动员攀爬金属梯级', '运动员在户外跑道奔跑'),
            ('运动类', '篮球比赛球员运球', '运动员攀爬金属梯级', '篮球运动员在球场运球'),
            ('运动类', '获奖运动员站在领奖台', '运动员攀爬金属梯级', '获奖运动员站在领奖台上举起奖杯'),
            ('展会', '展台产品近景特写', '工人用角磨机打磨金属管的特写', '展台上产品的近景特写'),
            ('展会', '展馆内展台全景', '品牌开场宣传图', '展馆内展台与观众通道全景'),
            ('年会', '晚宴餐桌与酒杯细节', '观众坐在圆桌前观看舞台烟花', '晚宴餐桌上酒杯与餐具细节'),
            ('明星活动', '明星嘉宾走红毯', '明星嘉宾在舞台唱歌', '明星嘉宾走在红毯上'),
            ('明星活动', '模特在秀场走秀', '模特站在舞台讲话', '模特在秀场T台上走秀'),
            ('发布会', '观众上手体验产品', '观众在产品展台前参观交流', '观众上手操作产品进行体验'),
            ('发布会', '记者持相机拍摄活动', '嘉宾在舞台上合影', '记者手持相机拍摄活动现场'),
            ('年会', '领导向员工颁发奖杯', '多名员工站在舞台互动', '领导向员工颁发奖杯'),
        ]
        for script_type, query, unrelated, matching in cases:
            with self.subTest(query=query):
                result = search_images_for_slot(
                    {'slot_index': 3, '画面描述': query, 'current_visual_authority': True},
                    {'脚本类型': script_type},
                    [{'image_id': image_id, '原图路径': '/' + image_id + '.png',
                      'asset_role': 'source_reference', '来源文件': 'anonymous.xlsx',
                      '来源 sheet': '镜头', '来源行': row}
                     for row, image_id in enumerate(('wrong', 'right'), 1)],
                    [{'image_id': image_id, 'caption': caption, 'tags': ['匿名样例'], 'brand_names': ['无']}
                     for image_id, caption in (('wrong', unrelated), ('right', matching))],
                    top_k=2,
                )
                candidates = {item['image_id']: item for item in result['candidates']}
                self.assertEqual(set(candidates), {'wrong', 'right'})
                self.assertEqual(candidates['wrong']['match_tier'], 'reference')
                self.assertFalse(candidates['wrong']['default_eligible'])
                self.assertTrue(candidates['wrong']['requires_human_review'])
                self.assertTrue(candidates['wrong']['missing'])
                self.assertEqual(candidates['right']['match_tier'], 'matched')
                self.assertTrue(candidates['right']['default_eligible'])
                self.assertEqual(result['candidates'][0]['image_id'], 'right')
                self.assertEqual(candidates['wrong']['来源行'], 1)

    def test_unknown_visual_query_needs_positive_full_description_evidence(self):
        captions = {'unrelated': '观众拍照', 'exact': '蓝色背景中呈现抽象流体形态',
                    'negated': '没有抽象流体形态，只有观众拍照'}
        result = search_images_for_slot(
            {'slot_index': 3, '画面描述': '抽象流体形态', 'current_visual_authority': True},
            {'脚本类型': '音乐节'},
            [{'image_id': key, '原图路径': '/' + key + '.png', 'asset_role': 'source_reference'}
             for key in captions],
            [{'image_id': key, 'caption': caption, 'tags': ['匿名样例'], 'brand_names': ['无']}
             for key, caption in captions.items()],
        )
        candidates = {item['image_id']: item for item in result['candidates']}
        self.assertEqual(len(candidates), 3)
        for key in ('unrelated', 'negated'):
            self.assertFalse(candidates[key]['default_eligible'])
            self.assertTrue(candidates[key]['requires_human_review'])
        self.assertTrue(candidates['exact']['default_eligible'])
        self.assertEqual(result['candidates'][0]['image_id'], 'exact')
