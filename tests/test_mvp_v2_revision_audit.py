from __future__ import annotations

from dataclasses import replace
import unittest

from mvp_v2.audit import audit_generation
from mvp_v2.client_quality import audit_client_visible_content
from mvp_v2.generation_stages import read_visual_binding
from mvp_v2.formal_image_workspace import ImageVersionMaterializer
from tests import test_mvp_v2_formal_revision_union as union_fixture


class RevisionTechnicalOrdinalTests(unittest.TestCase):
    def test_old_shot_ordinal_warns_but_real_unsupported_numbers_remain_blocked(self):
        rows = [{"镜号": "16", "画面描述": "第17镜，品牌落版与环境收束", "声音": "音乐"}]
        checks = audit_client_visible_content({}, rows, [], quality_profile="client-review")
        self.assertFalse(any(x["检查项"] == "client_unsupported_numeric_fact" for x in checks))
        self.assertTrue(any(x["检查项"] == "shot_reference_number_mismatch" and x["级别"] == "warning" for x in checks))
        rows[0]["画面描述"] = "第17镜，17位嘉宾在入口合影"
        checks = audit_client_visible_content({}, rows, [], quality_profile="client-review")
        self.assertTrue(any(x["检查项"] == "client_unsupported_numeric_fact" and x["级别"] == "hard" for x in checks))


class RevisionAuditParityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = union_fixture.FormalRevisionUnionTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.workspace = self.fixture.base.workspace
        self.scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.fixture.project_id}
        _, self.context = self.workspace._current_context(self.fixture.token, **self.scope)
        self.resolver = self.fixture.base.resolver
        self.binding = read_visual_binding(self.resolver.resolve_stage_artifact(
            self.context.run_id, "visual_binding", expected_manifest_sha256=self.context.manifest.sha256,
        ).path)
        self.workbook = self.resolver.resolve_artifact(self.context.run_id, "workbook").path
        self.source = self.resolver.resolve_template_workbook(
            self.context.run_id, expected_manifest_sha256=self.context.manifest.sha256,
        )

    def _generation_warnings(self):
        full = audit_generation(
            self.binding.brief, self.binding.template_slots, self.binding.decisions,
            self.binding.script_rows, self.binding.detail_rows,
            visual_asset_rows=self.binding.visual_asset_rows, require_complete_visuals=False,
        )
        warnings = {(x["检查项"], x["关联镜号"]) for x in full["checks"] if x["级别"] == "warning"}
        self.assertTrue(warnings)
        return warnings

    def test_script_save_recomputes_existing_quality_warnings(self):
        _, auditor = self.workspace.materializer._workbook_functions(
            self.binding, self.source, template_workbook_path=self.source,
        )
        result = auditor(self.binding, self.workbook)
        actual = {(x["检查项"], x["关联镜号"]) for x in result["checks"] if x["级别"] == "warning"}
        self.assertLessEqual(self._generation_warnings(), actual)
        self.assertLess(result["score"], 100)

    def test_image_save_recomputes_document_and_media_warnings(self):
        materializer = ImageVersionMaterializer(
            artifacts=self.resolver, runs_root=self.fixture.base.generated.root / "runs",
            source_root=self.source,
        )
        _, auditor = materializer._workbook_functions(
            self.binding, {}, self.source, template_workbook_path=self.source,
        )
        result = auditor(self.binding, self.workbook)
        actual = {(x["检查项"], x["关联镜号"]) for x in result["checks"] if x["级别"] == "warning"}
        self.assertLessEqual(self._generation_warnings(), actual)
        self.assertNotEqual(result["media_readiness"]["status"], "ready")

    def test_duration_revision_records_original_brief_deviation_without_faking_brief(self):
        rows = [dict(row) for row in self.binding.script_rows]
        rows[0]["时长(秒)"] = int(rows[0]["时长(秒)"]) + 1
        revised = replace(self.binding, script_rows=rows)
        renderer, auditor = self.workspace.materializer._workbook_functions(
            revised, self.source, template_workbook_path=self.source,
        )
        output = self.fixture.base.generated.root / "changed-duration.xlsx"
        renderer(revised, output, [])
        result = auditor(revised, output)
        checks = [x for x in result["checks"] if x["检查项"] == "duration_total_mismatch"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["级别"], "warning")
        self.assertIn("60", checks[0]["详情"])
        self.assertEqual(revised.brief, self.binding.brief)


if __name__ == "__main__":
    unittest.main()
