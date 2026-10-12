"""RV04-02 frozen coverage follows canonical shot identity after revision."""
import json
import unittest

from mvp_v2.version_delivery import resolve_version_shot_ids
from tests import test_mvp_v2_formal_script_workspace as fixture


class CoverageIdentityRecoveryTests(unittest.TestCase):
    def test_saved_edit_rebinds_fragments_without_mutating_parent_or_approvals(self):
        f = fixture.FormalScriptWorkspaceTests('runTest')
        f.setUp(); self.addCleanup(f.doCleanups)
        token = f.generated.token
        project_id = f.generated.project['project_id']
        scope = dict(workspace_id=f.generated.workspace_id, project_id=project_id)
        current = f.workspace.read_current(token, **scope)
        parent = f.delivery.resolve(token, project_id, current['base_version_id'])
        parent_paths = {stage: f.resolver.resolve_stage_artifact(parent.run_id, stage).path
            for stage in ('script_plan', 'script_draft', 'visual_binding')}
        parent_bytes = {stage: path.read_bytes() for stage, path in parent_paths.items()}
        raw = json.loads(parent_bytes['script_draft'])
        parent_ids = resolve_version_shot_ids(f.resolver, parent)
        affected = next(row for row in raw['traceability'] if row.get('output_evidence'))
        shot_no = affected['output_evidence'][0]['shot_no']
        with f.store.connection() as connection:
            reviews_before = [tuple(row) for row in connection.execute('SELECT * FROM version_quality_final_confirmations')]
        changed = f.workspace.apply_table_change(token, **scope,
            expected_project_version=current['project_version'], expected_revision=current['revision'],
            shot_no=shot_no, shot_id=parent_ids[shot_no], field='visual',
            value='匿名来宾沿通道进入会场。', idempotency_key='coverage-identity-edit')
        f.workspace.save_version(token, **scope,
            expected_project_version=current['project_version'], expected_revision=changed['revision'],
            reason='覆盖原文变更回归', idempotency_key='coverage-identity-save')
        project = f.projects.get_project(token, project_id)
        child = f.delivery.resolve(token, project_id, project['current_version_id'])
        child_ids = resolve_version_shot_ids(f.resolver, child)
        child_raw = json.loads(f.resolver.resolve_stage_artifact(child.run_id, 'script_draft').path.read_text())
        old_by_requirement = {row['requirement_id']: row for row in raw['traceability']}
        changed_evidence = 0
        for row in child_raw['traceability']:
            old_evidence = {item['shot_no']: item for item in old_by_requirement[row['requirement_id']].get('output_evidence', [])}
            for evidence in row.get('output_evidence', []):
                content = evidence['content_evidence']
                self.assertEqual(content['shot_id'], child_ids[evidence['shot_no']])
                self.assertEqual(content['requirement_id'], row['requirement_id'])
                self.assertEqual(content['verification_status'], 'pending_review')
                before = old_evidence[evidence['shot_no']]['content_evidence']
                if evidence['shot_no'] == shot_no:
                    changed_evidence += 1
                    self.assertNotEqual(content['script_sha256'], before['script_sha256'])
                    self.assertEqual(evidence['visual_description'], '匿名来宾沿通道进入会场。')
                    self.assertEqual(content['shot_id'], parent_ids[shot_no])
                else:
                    self.assertEqual(content['script_sha256'], before['script_sha256'])
                    self.assertEqual(content['fragments'], before['fragments'])
        self.assertGreater(changed_evidence, 0)
        self.assertEqual(parent_bytes, {stage: path.read_bytes() for stage, path in parent_paths.items()})
        with f.store.connection() as connection:
            self.assertEqual(reviews_before, [tuple(row) for row in connection.execute('SELECT * FROM version_quality_final_confirmations')])
