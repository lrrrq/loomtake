"""Read projections for the existing Formal agent, never a second conversation store.

Task events own original messages and operation receipts. Session metadata only
holds presentation preferences and a disposable, source-checked summary cache.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re

from .director_history import messages, history_context


_REFERENCE_CONTEXT_CHARS = 45000


def _reference_size(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _reference_readback(documents, instruction):
    """Select cited original passages once for both understanding and editing.

    The budget includes document identities, evidence and coverage metadata.
    Excerpts use zero-based, end-exclusive character ranges in the original
    location; they never change the source's content SHA or claim full reading.
    """
    def project(document, selected):
        locations = []
        read_characters = 0
        for index, ranges in sorted(selected.items()):
            original = document['locations'][index]
            text = str(original.get('text') or '')
            merged = []
            for start, end in sorted(ranges):
                if merged and start <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            for start, end in merged:
                location = {**original, 'text': text[start:end]}
                if start != 0 or end != len(text):
                    location['source_excerpt'] = {'location_index': index + 1,
                        'char_start': start, 'char_end': end, 'total_characters': len(text)}
                locations.append(location)
                read_characters += end - start
        total = sum(len(str(item.get('text') or '')) for item in document.get('locations', []))
        omitted = len(document.get('locations', [])) - len(selected)
        return {**document, 'locations': locations, 'omitted_location_count': omitted,
            'omitted_text_characters': total - read_characters,
            'context_status': 'partial' if omitted or total != read_characters else 'complete'}

    projected = [project(document, {i: [(0, len(str(location.get('text') or '')))]
        for i, location in enumerate(document.get('locations', []))}) for document in documents]
    if _reference_size(projected) <= _REFERENCE_CONTEXT_CHARS:
        return projected

    # Retain every selected document's identity when possible. A document whose
    # metadata alone exceeds the budget remains visible as unread in the source
    # catalogue below, rather than being misrepresented as fully consumed.
    projected, included = [], []
    for index, document in enumerate(documents):
        empty = project(document, {})
        if _reference_size([*projected, empty]) <= _REFERENCE_CONTEXT_CHARS:
            projected.append(empty)
            included.append(index)
    terms = set(re.findall(r'[a-z0-9_]{2,40}', instruction.casefold()))
    for phrase in re.findall(r'[\u3400-\u9fff]{2,}', instruction):
        terms.update(phrase[start:start + width] for width in (2, 3, 4)
                     for start in range(max(0, len(phrase) - width + 1)))
    requested_positions = [(key, int(number)) for key, suffix in
        (('page', '页'), ('paragraph', '段'), ('slide', '幻灯片'), ('row', '行'))
        for number in re.findall(r'第?\s*(\d+)\s*' + suffix, instruction)]
    candidates = []
    for output_index, document_index in enumerate(included):
        document = documents[document_index]
        for location_index, location in enumerate(document.get('locations', [])):
            text = str(location.get('text') or '')
            position = (location.get('evidence') or {}).get('location', {})
            explicit = sum(position.get(key) == number for key, number in requested_positions)
            for start in range(0, max(1, len(text)), 3800):
                end = min(len(text), start + 4000)
                passage = text[start:end].casefold()
                score = sum(len(term) ** 2 for term in terms if term in passage)
                candidates.append((explicit, score, -location_index, -start, -document_index,
                    output_index, location_index, start, end))
    selected = [{} for _ in projected]
    for *_, output_index, location_index, start, end in sorted(candidates, reverse=True):
        ranges = selected[output_index].setdefault(location_index, [])
        ranges.append((start, end))
        candidate = project(documents[included[output_index]], selected[output_index])
        previous = projected[output_index]
        projected[output_index] = candidate
        if _reference_size(projected) > _REFERENCE_CONTEXT_CHARS:
            projected[output_index] = previous
            ranges.pop()
            if not ranges:
                selected[output_index].pop(location_index)
    return projected


def project_context(agent, auth_token, agent_session_id, *, base=None, source_selection=None,
                    selection_explicit=False, instruction='', exclude_task_id='', for_model=False):
    session = agent.services.tasks.get_agent_session(auth_token, agent_session_id)
    project = agent.services.projects.get_project(auth_token, session['project_id'])
    raw = base if base is not None else agent._context(auth_token, project, agent_session_id='')
    raw = deepcopy(raw)
    knowledge = agent.project_knowledge.list_project_conventions(
        auth_token, project['project_id'], include_inactive=True)
    conventions = [item for item in knowledge['conventions'] if item['status'] == 'active']
    retired = knowledge.get('inactive_statements', [])
    chosen = source_selection if selection_explicit else session.get('selected_source_ids')
    pid = project['project_id']
    sources = []
    sections = (
        ('brief', '项目需求', 'brief', True),
        ('creative', '当前初稿与已选配图', 'script', True),
        ('script', '当前正式稿', 'script', True),
        ('project_images', '补充项目素材信息', 'materials', False),
        ('images', '配图进度与缺口信息', 'script?panel=images', False),
        ('review', '审核与待办', 'review', False),
    )
    for key, title, route, required in sections:
        section = raw.get(key)
        if not section:
            continue
        if key == 'creative' and not section.get('blocks'):
            continue
        if key == 'script' and not section.get('shots'):
            continue
        source_id = 'project:' + key
        selected = required or chosen is None or source_id in chosen
        count = section.get('shot_count', section.get('block_count'))
        summary = section.get('message') or section.get('version_label') or \
            (f'{count} 个镜头或段落' if count is not None else '从当前项目权威记录读取')
        if key == 'creative' and section.get('image_references'):
            summary = str(summary) + '；包含已保存的配图选择，稿件设想与参考图不证明客户现场事实'
        sources.append({'id': source_id, 'title': title, 'kind': key, 'required': required,
            'selected': selected, 'summary': str(summary), 'href': f'/projects/{pid}/{route}'})
        if not selected:
            raw.pop(key, None)
    reference_documents = []
    for document in raw.get('reference_documents', []):
        source_id = 'document:' + document['source_document_id']
        selected = chosen is None or source_id in chosen
        sources.append({'id': source_id, 'title': document.get('filename') or '项目资料', 'kind': 'document',
            'required': False, 'selected': selected, 'summary': {
                'failed': '读取失败，请在需求页查看原因或替换资料',
                'partial': '部分读取成功，未读部分不能作为依据',
            }.get(document.get('status'), '已读取资料，保留原文位置与文件版本'),
            'href': f'/projects/{pid}/brief', 'status': document.get('status'),
            'content_sha256': document.get('content_sha256'), 'brief_version_id': document.get('brief_version_id')})
        if not selected:
            continue
        reference_documents.append({**document, 'source_id': source_id})
    raw['reference_documents'] = _reference_readback(reference_documents, instruction)
    readbacks = {item['source_id']: item for item in raw['reference_documents']}
    partial_sources = 0
    for source in sources:
        if source['kind'] != 'document' or not source['selected']:
            continue
        readback = readbacks.get(source['id'])
        if readback is None or readback['context_status'] == 'partial':
            partial_sources += 1
            source.update(context_status='partial', status='partial',
                summary='本轮只回读部分原文，保留位置与片段范围；可指定页码、段落或关键词继续读取')
    raw['source_context_warnings'] = ([
        f'{partial_sources} 份所选资料仅部分进入本轮上下文。片段不是全文；遗漏内容不可作为已读取依据。'
    ] if partial_sources else [])
    if conventions:
        sources.append({'id': 'project:conventions', 'title': '有效项目约定', 'kind': 'conventions',
            'required': True, 'selected': True, 'summary': f'{len(conventions)} 条明确保存的创作约定',
            'href': f'/projects/{pid}/assistant'})
    raw.update(project_conventions=conventions,
        retired_conventions=retired,
        excluded_source_ids=[item['id'] for item in sources if not item['selected']],
        knowledge_version=knowledge['knowledge_version'],
        sources=[item for item in sources if item['selected']])
    if for_model:
        raw.update(history_context(agent, auth_token, agent_session_id, instruction=instruction,
            exclude_task_id=exclude_task_id, retired=retired,
            excluded_source_ids=raw['excluded_source_ids']))
        # The visible source catalogue and the advisor's citation allow-list
        # describe the same authorized projection. A selected document or an
        # active convention must not be rejected merely for using its public ID.
        raw['evidence_ids'] = sorted({*raw.get('evidence_ids', []),
            *(item['id'] for item in raw['sources'])})
        return raw
    return {'context_sources': sources, 'project_decisions': knowledge['conventions'],
        'knowledge_version': knowledge['knowledge_version'], 'read_errors': raw.get('read_errors', []),
        'project': raw.get('project', {}), 'project_id': pid,
        'stage': 'formal' if (raw.get('script') or {}).get('shots') else
                 'creative' if (raw.get('creative') or {}).get('blocks') else 'discussion',
        'history_available': True}
