"""Dynamic positive Brief durations and evidence-preserving planning projection.

Anonymous in-memory contracts only; no database, UI or Provider calls.
"""
from copy import deepcopy
from hashlib import sha256
import json
import unittest

from mvp_v2 import models
from mvp_v2.formal_brief_domain import ArtifactPayload, ConfirmedBriefInput, FORMAL_BRIEF_SCHEMA
from mvp_v2.generation_stages import _confirmed_brief_evidence, _planning_brief_from_confirmed_payload


class DynamicDurationTests(unittest.TestCase):
    @staticmethod
    def confirmed_payload(duration):
        values = {'项目名称': '匿名动态时长发布会', '脚本类型': '发布会', '目标时长': duration}
        payload = {'facts': [{'fact_id': f'fact_{index}', 'field_path': field,
                    'value_json': json.dumps(value, ensure_ascii=False), 'certainty': 'confirmed',
                    'evidence_refs': [{'artifact_ref': 'artifact://evidence/anonymous-duration',
                                       'source_document_id': 'anonymous-duration',
                                       'location': {'page': 1, 'paragraph': index}}]}
                   for index, (field, value) in enumerate(values.items(), 1)]}
        envelope = {'schema_version': FORMAL_BRIEF_SCHEMA, 'workspace_id': 'workspace-anonymous',
                    'project_id': 'project-anonymous', 'brief_version_id': 'brief-v1', 'brief': payload}
        raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode()
        confirmed = ConfirmedBriefInput(workspace_id='workspace-anonymous', project_id='project-anonymous',
            brief_version_id='brief-v1', confirmation_id='confirmation-v1', brief_snapshot_id='snapshot-v1',
            payload=ArtifactPayload('artifact://brief/anonymous-duration.json', sha256(raw).hexdigest()),
            confirmation_status='confirmed', unresolved_conflict_count=0,
            confirmed_by_actor_id='actor-anonymous', confirmed_at='2026-10-09T00:00:00Z')
        return payload, _confirmed_brief_evidence(confirmed)

    def test_constraint_helper_distinguishes_explicit_and_inferred_total(self):
        self.assertEqual(models.parse_duration_constraints('整片56秒，共7镜，每镜8秒', infer_total=False),
                         {'total': {56}, 'shot_count': {7}, 'per_shot': {8}})
        self.assertEqual(models.parse_duration_constraints('共7镜，每镜8秒', infer_total=False),
                         {'shot_count': {7}, 'per_shot': {8}})
        self.assertEqual(models.parse_duration_constraints('共7镜，每镜8秒', infer_total=True),
                         {'total': {56}, 'shot_count': {7}, 'per_shot': {8}})
        self.assertEqual(models.parse_duration_constraints('整片56秒，共7镜，每镜9秒', infer_total=True),
                         {'total': {56, 63}, 'shot_count': {7}, 'per_shot': {9}})

    def test_invalid_explicit_total_cannot_be_repaired_by_shot_arithmetic(self):
        for value in ('56.5', '-56', '0'):
            with self.subTest(value=value):
                text = f'整片{value}秒，共7镜，每镜8秒'
                explicit = models.parse_duration_constraints(text, infer_total=False)
                inferred = models.parse_duration_constraints(text, infer_total=True)
                self.assertEqual(explicit, {'total': {0}, 'shot_count': {7}, 'per_shot': {8}})
                self.assertEqual(inferred, {'total': {0, 56}, 'shot_count': {7}, 'per_shot': {8}})

    def test_ordinal_shots_and_quoted_copy_do_not_supply_shot_count(self):
        for ordinal in ('第1镜', '第12镜', '第 7 镜'):
            with self.subTest(ordinal=ordinal):
                parsed = models.parse_duration_constraints(f'整片56秒，{ordinal}是片尾，每镜8秒')
                self.assertEqual(parsed, {'total': {56}, 'per_shot': {8}})
        self.assertEqual(models.parse_duration_constraints('整片56秒，片尾原文“总60秒，共8镜，每镜9秒”'),
                         {'total': {56}})

    def test_per_shot_duration_label_is_not_an_explicit_total(self):
        self.assertEqual(models.parse_duration_constraints('共7镜，每镜时长8秒', infer_total=False),
                         {'shot_count': {7}, 'per_shot': {8}})
        payload, evidence = self.confirmed_payload('共7镜，每镜时长8秒')
        with self.assertRaises(ValueError):
            _planning_brief_from_confirmed_payload(payload, evidence)
        payload, evidence = self.confirmed_payload('整片56秒，共7镜，每镜时长8秒')
        brief, _ = _planning_brief_from_confirmed_payload(payload, evidence)
        self.assertEqual(brief['视频时长'], '56s')

    def test_launch_and_general_validation_accept_dynamic_integer_seconds(self):
        for duration in (56, 60, 61):
            for suffix in ('秒', 's'):
                brief = {'脚本类型': '发布会', '视频时长': f'{duration}{suffix}'}
                with self.subTest(duration=duration, suffix=suffix):
                    self.assertEqual(models.validate_launch_brief(brief), duration)
                    self.assertEqual(models.validate_script_brief(brief), duration)

    def test_strict_parser_and_validators_still_reject_non_scalar_or_nonpositive_duration(self):
        for value in ('', '0秒', '-56秒', '56.5秒', '整片56秒，共7镜，每镜8秒', '56秒或60秒'):
            with self.subTest(value=value):
                self.assertIsNone(models.parse_positive_seconds_strict(value))
                for validator in (models.validate_launch_brief, models.validate_script_brief):
                    with self.assertRaises(ValueError):
                        validator({'脚本类型': '发布会', '视频时长': value})

    def test_confirmed_rich_duration_projects_only_total_and_keeps_source_evidence(self):
        for value, total in (
            ('整片56秒，共7镜，每镜8秒。片尾包含在56秒内', 56),
            ('每镜8秒，共7镜，总56秒', 56),
            ('整片60秒，共10镜，每镜6秒', 60),
            ('61秒', 61),
        ):
            with self.subTest(value=value):
                payload, evidence = self.confirmed_payload(value)
                original = deepcopy(payload)
                evidence_before = deepcopy(evidence)
                brief, provenance = _planning_brief_from_confirmed_payload(payload, evidence)
                self.assertEqual(brief['视频时长'], value if models.parse_positive_seconds_strict(value) else f'{total}s')
                self.assertEqual(models.validate_script_brief(brief), total)
                self.assertEqual(payload, original)
                self.assertEqual(evidence, evidence_before)
                duration_fact = next(item for item in payload['facts'] if item['field_path'] == '目标时长')
                duration_provenance = next(item for item in provenance if item['field_path'] == '目标时长')
                self.assertEqual(duration_provenance['fact_id'], duration_fact['fact_id'])
                self.assertEqual(duration_provenance['evidence_refs'], duration_fact['evidence_refs'])
                self.assertEqual(brief['_formal_fact_evidence']['视频时长']['evidence_refs'],
                                 duration_fact['evidence_refs'])
                self.assertEqual(brief['Brief来源 SHA256'], evidence['payload']['sha256'])
                self.assertEqual(json.loads(duration_fact['value_json']), value)

    def test_planning_rejects_ambiguous_inferred_invalid_or_contradictory_total(self):
        values = ('总时长56秒，整片60秒', '共7镜，每镜8秒', '0秒', '-56秒', '56.5秒',
                  '整片0秒，共7镜，每镜8秒', '整片-56秒，共7镜，每镜8秒',
                  '整片56.5秒，共7镜，每镜8秒', '整片56秒，共7镜，每镜9秒')
        for value in values:
            with self.subTest(value=value):
                payload, evidence = self.confirmed_payload(value)
                original = deepcopy(payload)
                with self.assertRaises(ValueError) as rejected:
                    _planning_brief_from_confirmed_payload(payload, evidence)
                self.assertEqual(getattr(rejected.exception, 'code', None),
                                 'confirmed_brief_needs_clarification')
                self.assertEqual(payload, original)
