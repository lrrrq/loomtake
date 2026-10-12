import json
import unittest
from pathlib import Path

from mvp_v2.script_type_adapter import (
    LAUNCH_SCRIPT_TYPE_METADATA,
    ScriptTypeAdapter,
)
from mvp_v2.stage_contract import (
    STAGE_DEPENDENCIES,
    STAGE_OPTIONAL_DEPENDENCIES,
    STAGE_ORDER,
    StageError,
    StageErrorCategory,
    StageId,
    StageManifest,
    StageStatus,
)
from mvp_v2.public_contract import PublicLifecycleStatus


class DummyAdapter:
    category_id = "launch"

    def parse_source_workbook(self, workbook_path: Path):
        return [{"source_workbook": workbook_path.name, "slot_index": 1}]

    def validate_source_workbook(self, workbook_path: Path, source_records):
        return []

    def shot_count(self, source_records):
        return len(source_records)

    def generation_rules(self):
        return {"preserve_template_order": True}

    def audit_generation(self, brief, source_records, decisions, script_rows, detail_rows):
        return {"status": "pass", "shot_count": len(script_rows)}


class StageContractTests(unittest.TestCase):
    def test_stage_ids_and_statuses_are_frozen(self):
        self.assertEqual(
            tuple(stage_id.value for stage_id in STAGE_ORDER),
            (
                "input_freeze",
                "source_catalog",
                "media_manifest",
                "visual_analysis",
                "script_plan",
                "script_draft",
                "visual_binding",
                "workbook_render",
                "machine_audit",
                "human_review",
                "publish_feedback",
            ),
        )
        self.assertEqual(
            tuple(status.value for status in StageStatus),
            (
                "not_started",
                "running",
                "succeeded",
                "succeeded_with_warnings",
                "blocked",
                "failed",
                "waiting_human_review",
                "invalidated",
            ),
        )
        self.assertEqual(
            tuple(category.value for category in StageErrorCategory),
            ("input", "dependency", "contract", "execution", "system"),
        )
        self.assertEqual(
            {"running", "blocked", "failed", "waiting_human_review"},
            {status.value for status in PublicLifecycleStatus} & {status.value for status in StageStatus},
        )

    def test_dependencies_are_complete_and_topological(self):
        self.assertEqual(set(STAGE_DEPENDENCIES), set(STAGE_ORDER))
        positions = {stage_id: index for index, stage_id in enumerate(STAGE_ORDER)}
        for stage_id, dependencies in STAGE_DEPENDENCIES.items():
            with self.subTest(stage_id=stage_id.value):
                self.assertTrue(all(positions[item] < positions[stage_id] for item in dependencies))
        self.assertEqual(
            STAGE_DEPENDENCIES[StageId.SCRIPT_PLAN],
            (StageId.SOURCE_CATALOG,),
        )
        self.assertEqual(
            STAGE_OPTIONAL_DEPENDENCIES[StageId.VISUAL_BINDING],
            (StageId.VISUAL_ANALYSIS,),
        )
        self.assertEqual(
            STAGE_DEPENDENCIES[StageId.HUMAN_REVIEW],
            (StageId.MACHINE_AUDIT,),
        )

    def test_stage_manifest_is_json_serializable(self):
        manifest = StageManifest(
            run_id="run-001",
            category_id="launch",
            stage_id=StageId.VISUAL_ANALYSIS,
            status=StageStatus.BLOCKED,
            attempt=2,
            input_sha256="a" * 64,
            output_sha256="b" * 64,
            started_at="2026-07-30T10:00:00Z",
            finished_at="2026-07-30T10:01:00Z",
            log_ref="logs://run-001/visual_analysis/2",
            input_refs=("manifest://media",),
            error=StageError(
                category=StageErrorCategory.DEPENDENCY,
                code="media_not_ready",
                message="media manifest is not ready",
                retryable=True,
            ),
        )

        payload = json.loads(json.dumps(manifest.to_dict()))

        self.assertEqual(payload["schema_version"], "1")
        self.assertEqual(payload["stage_id"], "visual_analysis")
        self.assertEqual(payload["depends_on"], ["media_manifest"])
        self.assertEqual(payload["optional_depends_on"], [])
        self.assertEqual(payload["input_sha256"], "a" * 64)
        self.assertEqual(payload["output_sha256"], "b" * 64)
        self.assertEqual(payload["started_at"], "2026-07-30T10:00:00Z")
        self.assertEqual(payload["finished_at"], "2026-07-30T10:01:00Z")
        self.assertEqual(payload["log_ref"], "logs://run-001/visual_analysis/2")
        self.assertEqual(payload["error"]["category"], "dependency")
        self.assertTrue(payload["error"]["retryable"])

    def test_failed_or_blocked_manifest_requires_structured_error(self):
        for status in (StageStatus.BLOCKED, StageStatus.FAILED):
            with self.subTest(status=status.value):
                with self.assertRaises(ValueError):
                    StageManifest(
                        run_id="run-001",
                        category_id="launch",
                        stage_id=StageId.SCRIPT_PLAN,
                        status=status,
                    )

    def test_script_type_adapter_surface_and_launch_metadata(self):
        adapter = DummyAdapter()

        self.assertIsInstance(adapter, ScriptTypeAdapter)
        records = adapter.parse_source_workbook(Path("launch.xlsx"))
        self.assertEqual(adapter.shot_count(records), 1)
        self.assertEqual(LAUNCH_SCRIPT_TYPE_METADATA.category_id, "launch")
        self.assertEqual(
            LAUNCH_SCRIPT_TYPE_METADATA.source_workbook_name,
            "M+发布会类通用脚本库.xlsx",
        )
        self.assertTrue(LAUNCH_SCRIPT_TYPE_METADATA.dynamic_shot_count)


if __name__ == "__main__":
    unittest.main()
