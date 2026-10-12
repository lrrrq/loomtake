import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp_v2.formal_image_workspace import ImageVersionMaterializer
from mvp_v2.formal_script_workspace import DraftVersionMaterializer


class RevisionImageEvidenceTests(unittest.TestCase):
    def test_deleted_shot_does_not_keep_its_analysis_as_current_image_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "parent.json"
            analyses = [{"image_id": f"image-{i}", "caption": f"匿名图片{i}", "source": "manual_seed"} for i in range(1, 4)]
            payload = {"brief": {"脚本类型": "明星活动"}, "image_analysis_rows": analyses,
                       "script_rows": [{"镜号": str(i)} for i in range(1, 4)]}
            parent.write_text(json.dumps(payload))
            plan = root / "child-plan.json"
            plan.write_text(json.dumps({"manual_revision": {"origin_to_child": {"1": "1", "3": "2"}},
                                        "decisions": [], "template_slots": [], "traceability": []}))
            with patch("mvp_v2.formal_script_workspace.start_pipeline_stage"), patch("mvp_v2.formal_script_workspace.finish_pipeline_stage"):
                output = DraftVersionMaterializer._write_binding_stage(
                    SimpleNamespace(run_dir=root), parent=parent, child_plan_path=plan, child_draft_path=plan,
                    script_rows=[{"镜号": str(i), "画面描述": "匿名场景"} for i in (1, 2)],
                    detail_rows=[{"模板镜号": str(i)} for i in (1, 2)],
                    bindings=[{"shot_no": str(i), "image_id": f"image-{origin}"} for i, origin in ((1, 1), (2, 3))],
                    visual_asset_rows=[{"slot_index": i, "image_id": f"image-{origin}", "visual_asset_id": f"asset-{origin}"} for i, origin in ((1, 1), (2, 3))],
                    plan_constraints={}, request_sha256="a"*64,
                )
            result = json.loads(output.read_text())
            self.assertEqual(result["image_analysis_rows"], [analyses[0], analyses[2]])
            self.assertEqual(json.loads(parent.read_text()), payload)

    def test_confirmed_selection_replaces_old_analysis_without_claiming_model_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "parent.json"
            payload = {
                "brief": {"脚本类型": "明星活动"},
                "script_rows": [{"镜号": str(i), "画面描述": "来宾在入口签到", "结构": "签到"} for i in range(1, 4)],
                "detail_rows": [{"slot_index": i, "采用来源文件": "旧模板.xlsx", "采用来源 sheet": "模板", "采用来源行": 20+i,
                                 "视觉内容摘要": "旧图产品体验", "视觉标签": "旧图", "图片绑定说明": "旧图绑定",
                                 "image_id": "old-image", "原图路径": "old-image.jpg"} for i in range(1, 4)],
                "image_analysis_rows": [{"image_id": "old-image", "caption": "旧图"}],
            }
            parent.write_text(json.dumps(payload), encoding="utf-8")
            decisions = [{
                "shot_no": str(i), "decision_kind": "image_confirmed", "image_id": f"new-{i}",
                "image_sha256": str(i)*64, "source_key": f"doc-{i}",
                "source_location": {"file": "新图片.png", "sheet": "图片", "row": i},
                "summary": "观众在入口登记", "tags": ["观众", "登记"], "quality": "清晰",
                "analysis": {"analysis_origin": "manual", "caption": "观众在入口登记", "tags": ["观众", "登记"],
                             "brand": "无", "quality_flags": [], "negative_flags": [],
                             "reviewed_by_actor_id": "manager", "reviewed_at": "2026-09-09T00:00:00Z"},
            } for i in range(1, 4)]
            with patch("mvp_v2.formal_image_workspace.start_pipeline_stage"), patch("mvp_v2.formal_image_workspace.finish_pipeline_stage"):
                output = ImageVersionMaterializer._write_binding_stage(
                    SimpleNamespace(run_dir=root), parent=parent, script_plan_path=parent, script_draft_path=parent,
                    child_bindings=[], decisions=decisions, request_sha256="a"*64,
                )
            result = json.loads(output.read_text())
            for i, detail in enumerate(result["detail_rows"], 1):
                self.assertEqual(detail["视觉内容摘要"], "观众在入口登记")
                self.assertEqual(detail["image_id"], f"new-{i}")
                self.assertEqual(detail["采用来源行"], 20+i)
                self.assertNotIn("旧图", detail["图片绑定说明"])
                self.assertNotEqual(detail["视觉读取状态"], "pass")
            self.assertEqual([x["image_id"] for x in result["image_analysis_rows"]], ["new-1", "new-2", "new-3"])
            self.assertTrue(all(x["source"] == "manual_seed" for x in result["image_analysis_rows"]))
            self.assertEqual(json.loads(parent.read_text()), payload)
