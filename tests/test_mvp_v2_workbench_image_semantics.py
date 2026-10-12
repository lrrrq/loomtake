import json
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest

from mvp_v2.image_retrieval import assess_image_candidates, search_images_for_slot


def _pool():
    media = [{'image_id': 'entry', 'content_sha256': 'a' * 64,
              '原图路径': '/anonymous/entry.png', 'asset_role': 'source_reference',
              'source_links': [{'source_file': 'anonymous.xlsx', 'source_sheet': '创意库', 'source_row': 9}],
              'source_semantic_text': '活动入口构图参考'}]
    analysis = [{'image_id': 'entry', 'content_sha256': 'a' * 64, 'caption': '来宾步入无可见品牌的入口大厅',
                 'tags': ['来宾', '入口'], 'main_subject': '来宾', 'foreground_subjects': ['来宾'],
                 'background_subjects': ['大厅'], 'camera_angle': '平视', 'uncertainties': ['未能识别远处文字'],
                 'brand_names': ['无'], 'negative_flags': [], 'quality_flags': []}]
    return media, analysis


def test_neutral_visual_does_not_require_customer_logo_and_rich_observations_reach_model(tmp_path):
    media, analysis = _pool()
    captured = []
    def writer(prompt, *_):
        captured.append(json.loads(prompt.split('\nINPUT:', 1)[1]))
        return json.dumps({'matches': [{'image_id': 'entry', 'score': 92, 'hits': ['来宾入场'],
                                       'missing': [], 'conflicts': [], 'reason': '实际人物动作与场景符合。'}]})
    slot = {'画面描述': '来宾进入入口大厅', 'current_visual_authority': True}
    brief = {'脚本类型': '年会', '品牌或客户': '蔡司', '项目名称': '客户活动',
             '必须出现的内容': '新品亮相', '禁忌或不能出现的内容': '烟草'}
    policy = SimpleNamespace(writer=writer, writer_model='anonymous', writer_provider='anonymous')
    assessment = assess_image_candidates(slot, brief, media, analysis, policy, tmp_path)
    candidate_input = captured[0]['candidates'][0]
    assert captured[0]['brief'] == {'脚本类型': '年会', '禁忌或不能出现的内容': '烟草'}
    assert candidate_input['requested_brands'] == []
    for name in ('main_subject', 'foreground_subjects', 'background_subjects', 'camera_angle', 'uncertainties'):
        assert candidate_input['visual_analysis'][name] == analysis[0][name]
    result = search_images_for_slot(slot, brief, media, analysis, semantic_assessment=assessment)
    candidate = result['candidates'][0]
    assert candidate['suggestion_eligible'] is True
    assert candidate['suggestion_kind'] == 'primary_reference'
    assert candidate['default_eligible'] is False
    assert candidate['requires_human_review'] is True


def test_unbound_or_mismatched_observations_cannot_become_default_suggestions():
    media, analysis = _pool()
    slot = {'画面描述': '来宾进入入口大厅', 'current_visual_authority': True}
    semantic = {'status': 'ready', 'matches': [{'image_id': 'entry', 'score': 99, 'hits': ['来宾入场'],
                  'missing': [], 'conflicts': [], 'reason': '语义相似。'}]}
    for digest in ('', 'b' * 64):
        analysis[0]['content_sha256'] = digest
        result = search_images_for_slot(slot, {'脚本类型': '年会'}, media, analysis, semantic_assessment=semantic)
        assert result['candidates'][0]['suggestion_eligible'] is False
        assert result['candidates'][0]['visual_evidence_bound'] is False


def test_explicit_visual_brand_aliases_do_not_depend_on_brief_customer():
    media, analyses = _pool()
    analyses[0].update(caption='蔡司蓝色入口装置，清晰可见 ZEISS 标识',
                       tags=['入口', 'ZEISS'], brand_names=['ZEISS', '蔡司'], ocr_text='ZEISS')
    semantic = {'status': 'ready', 'matches': [{'image_id': 'entry', 'score': 95,
        'hits': ['蓝色入口', 'ZEISS'], 'missing': [], 'conflicts': [],
        'reason': '主体与品牌均有视觉依据。', 'brand_checks': [
            {'requested_brand': 'ZEISS', 'observed_name': 'ZEISS', 'relation': 'same'}]}]}
    slot = {'画面描述': 'ZEISS 标识的蓝色入口装置', 'current_visual_authority': True}
    candidate = search_images_for_slot(slot, {'脚本类型': '年会'}, media, analyses,
                                      semantic_assessment=semantic)['candidates'][0]
    assert candidate['brand_warning'] == ''
    assert candidate['suggestion_eligible'] is True
    assert candidate['default_eligible'] is False
    assert candidate['requires_human_review'] is True
    analyses[0].update(caption='COLMO 蓝色入口装置', tags=['入口', 'COLMO'],
                       brand_names=['COLMO'], ocr_text='COLMO')
    candidate = search_images_for_slot(slot, {'脚本类型': '年会'}, media, analyses,
                                      semantic_assessment=semantic)['candidates'][0]
    assert candidate['brand_warning']
    assert candidate['suggestion_eligible'] is False


def test_explicit_visible_brand_stays_required_and_blocks_unproven_brand(tmp_path):
    media, analysis = _pool()
    captured = []
    def writer(prompt, *_):
        captured.append(json.loads(prompt.split('\nINPUT:', 1)[1]))
        return json.dumps({'matches': [{'image_id': 'entry', 'score': 99, 'hits': ['入口'],
            'missing': [], 'conflicts': [], 'reason': '模型试图忽略品牌要求。'}]})
    slot = {'画面描述': '蔡司品牌装置与清晰 ZEISS 标识占据画面中心', 'current_visual_authority': True}
    brief = {'脚本类型': '年会', '品牌或客户': '蔡司'}
    assessment = assess_image_candidates(slot, brief, media, analysis,
        SimpleNamespace(writer=writer, writer_model='anonymous', writer_provider='anonymous'), tmp_path)
    assert '蔡司' in captured[0]['candidates'][0]['requested_brands']
    result = search_images_for_slot(slot, brief, media, analysis, semantic_assessment=assessment)
    assert result['candidates'][0]['suggestion_eligible'] is False


def test_explicit_visual_brand_is_required_without_customer_or_source_brand(tmp_path):
    media, analyses = _pool()
    captured = []
    def writer(prompt, *_):
        captured.append(json.loads(prompt.split('\nINPUT:', 1)[1]))
        return json.dumps({'matches': [{'image_id': 'entry', 'score': 96,
            'hits': ['蓝色入口大厅'], 'missing': [], 'conflicts': [], 'brand_checks': [],
            'reason': '提供了场景但遗漏了品牌核对。'}]})
    slot = {'画面描述': 'ZEISS 标识清晰可见的蓝色入口大厅', 'current_visual_authority': True}
    brief = {'脚本类型': '年会'}
    assessment = assess_image_candidates(slot, brief, media, analyses,
        SimpleNamespace(writer=writer, writer_model='anonymous', writer_provider='anonymous'), tmp_path)
    assert captured[0]['candidates'][0]['requested_brands'] == ['ZEISS']
    assert assessment['status'] == 'ready'
    assert assessment['matches'][0]['brand_checks'][0]['relation'] == 'uncertain'
    candidate = search_images_for_slot(slot, brief, media, analyses,
                                      semantic_assessment=assessment)['candidates'][0]
    assert candidate['suggestion_eligible'] is False
    assert candidate['missing'] or candidate['conflicts']


def _recall_pool(scene, distractor_count=224):
    if scene == 'corridor':
        request = '第一人称视角沿会场入口的蓝色连续镂空拱门通道前行，白色地面与层层门洞形成纵深。'
        observed = {
            'caption': '日光下蓝色连续镂空拱门层层延伸，白色地面通往远端草坪。',
            'main_subject': '蓝色拱门入口通道',
            'foreground_subjects': ['近处镂空门洞', '白色地面'],
            'background_subjects': ['沿通道纵深排列的门洞', '远端草坪'],
            'scene_type': '连续拱门空间', 'camera_angle': '平视',
            'tags': ['连续门洞', '纵深空间'],
        }
        unrelated = '航拍户外大型活动会场全景，展示舞台与周围建筑轮廓，远景记录场地规模。'
        unrelated_tags = ['航拍', '活动会场', '建筑', '远景']
    else:
        request = '员工在彩色主题装置前自然互动并合影，轻松交谈、微笑和比拇指，记录多人真实姿态与年轻化打卡氛围。'
        observed = {
            'caption': '六位年轻人在彩色热气球装置旁轻松交谈、微笑和比拇指，一起面对镜头合影。',
            'main_subject': '六位年轻人自然合影',
            'foreground_subjects': ['自然交谈的多人', '比拇指的手势'],
            'background_subjects': ['彩色主题装置', '草坪'],
            'scene_type': '装置前多人合影', 'camera_angle': '平视',
            'tags': ['多人', '自然合影', '微笑'],
        }
        unrelated = '室内发布会签到墙前只有一名主持人讲解互动体验流程，身后大屏幕播放团队集体合影海报。'
        unrelated_tags = ['签到墙', '互动体验', '集体合影海报', '主持人']
    rows, analyses = [], []

    def add(identity, visual, source_description, *, forbidden=False):
        digest = sha256(identity.encode()).hexdigest()
        source = {'source_file': 'anonymous-visual-library.xlsx', 'source_sheet': '创意库',
            'source_row': len(rows) + 7, 'source_sha256': 'e' * 64, 'visual_desc': source_description,
            'generic_desc': source_description, 'visual_subject': source_description}
        rows.append({'image_id': identity, 'content_sha256': digest, '原图路径': f'/anonymous/{identity}.png',
            'asset_role': 'source_reference', '来源文件': source['source_file'], '来源 sheet': source['source_sheet'],
            '来源行': source['source_row'], 'source_links': [source], 'source_semantic_text': source_description,
            'package_sources': [{'package_id': 'anonymous-reference-library', 'package_version': 2,
                'package_manifest_sha256': 'f' * 64, 'media_id': identity,
                'sources': [{'file': source['source_file'], 'sheet': source['source_sheet'], 'row': source['source_row']}]}]})
        analyses.append({'image_id': identity, 'content_sha256': digest, 'brand_names': ['无'], 'ocr_text': '',
            'quality_flags': [], 'negative_flags': ['高风险禁用'] if forbidden else [], **deepcopy(visual)})

    for index in range(distractor_count):
        add(f'distractor-{index:03d}', {'caption': unrelated, 'tags': unrelated_tags}, '普通活动场面参考')
    add('observed-target', observed, '实拍空间或人物构图参考')
    add('source-claims-target', {'caption': '无人餐桌上摆着白色碗碟，固定近景只见餐具。',
        'main_subject': '餐具', 'tags': ['餐桌', '碗碟']}, request)
    add('forbidden-observed-target', observed, request, forbidden=True)
    return {'画面描述': request, 'current_visual_authority': True}, rows, analyses


def _capture_recalled_candidates(slot, media, analysis, tmp_path):
    captured = []

    def writer(prompt, *_):
        value = json.loads(prompt.split('\nINPUT:', 1)[1])
        captured.append(value)
        identities = {candidate['image_id'] for candidate in value['candidates']}
        matches = []
        if 'observed-target' in identities:
            matches.append({'image_id': 'observed-target', 'score': 94, 'hits': ['已观察到请求的核心主体与空间关系'],
                'missing': [], 'conflicts': [], 'reason': '依据实际图片分析中的主体、动作与空间关系判断。', 'brand_checks': []})
        if 'source-claims-target' in identities:
            matches.append({'image_id': 'source-claims-target', 'score': 18, 'hits': [],
                'missing': ['所要求的主体与空间未在图片中观察到'], 'conflicts': [],
                'reason': '来源文字虽然描述目标场景，实际图片分析只有餐桌和碗碟。', 'brand_checks': []})
        return json.dumps({'matches': matches}, ensure_ascii=False)

    policy = SimpleNamespace(writer=writer, writer_provider='anonymous', writer_model='anonymous')
    assessment = assess_image_candidates(slot, {'脚本类型': '年会'}, media, analysis, policy, tmp_path)
    assert assessment['status'] == 'ready'
    assert len(captured) == 1
    return assessment, captured[0]


@pytest.mark.parametrize('scene', ['corridor', 'natural_group_photo'])
def test_observed_scene_below_200_coarse_matches_reaches_bounded_model_input_with_provenance(scene, tmp_path):
    slot, media, analysis = _recall_pool(scene)
    legacy = search_images_for_slot({**slot, 'library_browse': True}, {'脚本类型': '年会'},
        media, analysis, top_k=len(media))
    legacy_ids = [candidate['image_id'] for candidate in legacy['candidates']]
    assert legacy_ids.index('observed-target') >= 200, 'Fixture must reproduce the prior coarse-function shortlist omission'
    assert 'forbidden-observed-target' not in legacy_ids

    assessment, payload = _capture_recalled_candidates(slot, media, analysis, tmp_path)
    ids = [candidate['image_id'] for candidate in payload['candidates']]
    assert 'observed-target' in ids
    assert 'forbidden-observed-target' not in ids
    assert len(ids) == len(set(ids)) <= 96
    assert len(json.dumps(payload, ensure_ascii=False, separators=(',', ':'))) <= 60000
    assert payload['scope']['safe_inventory_count'] == len(media) - 1
    by_id = {row['image_id']: row for row in media}
    for candidate in payload['candidates']:
        original = by_id[candidate['image_id']]
        assert candidate['content_sha256'] == candidate['visual_analysis']['content_sha256'] == original['content_sha256']
        assert len(candidate['source_record_ids']) == 1
        source = payload['source_records'][candidate['source_record_ids'][0]]
        for key in ('source_file', 'source_sheet', 'source_row', 'source_sha256'):
            assert source[key] == original['source_links'][0][key]
    result = search_images_for_slot(slot, {'脚本类型': '年会'}, media, analysis,
        semantic_assessment=assessment, top_k=len(media))
    assert {candidate['image_id'] for candidate in result['candidates'] if candidate['suggestion_eligible']} == {'observed-target'}
    observed = next(candidate for candidate in result['candidates'] if candidate['image_id'] == 'observed-target')
    assert observed['default_eligible'] is False
    assert observed['requires_human_review'] is True
    assert any(candidate['image_id'] == 'forbidden-observed-target' for candidate in result['rejected'])


def test_source_description_can_recall_a_candidate_but_cannot_certify_its_missing_visual_content(tmp_path):
    slot, media, analysis = _recall_pool('corridor', distractor_count=20)
    assessment, payload = _capture_recalled_candidates(slot, media, analysis, tmp_path)
    claimed = next(candidate for candidate in payload['candidates'] if candidate['image_id'] == 'source-claims-target')
    assert claimed['source_semantic_text'] == slot['画面描述']
    assert claimed['visual_analysis']['caption'] == '无人餐桌上摆着白色碗碟，固定近景只见餐具。'
    result = search_images_for_slot(slot, {'脚本类型': '年会'}, media, analysis,
        semantic_assessment=assessment, top_k=len(media))
    candidate = next(candidate for candidate in result['candidates'] if candidate['image_id'] == 'source-claims-target')
    assert candidate['missing']
    assert candidate['suggestion_eligible'] is False
    assert candidate['default_eligible'] is False
    assert candidate['requires_human_review'] is True
