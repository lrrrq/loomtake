from types import SimpleNamespace
import unittest

from mvp_v2.formal_review_workspace import FormalReviewWorkspaceService


class ReviewPrerequisiteTests(unittest.TestCase):
    def test_saved_images_are_not_counted_as_missing_and_progress_is_distinguished(self):
        service = object.__new__(FormalReviewWorkspaceService)
        shots = [{"shot_no": str(i), "shot_id": f"shot-{i}", "confirmed": i > 2,
                  "selected_image_key": f"image-{i}" if i > 2 else "", "candidates": []}
                 for i in range(1, 11)]
        result = service._blocked(
            {"name": "匿名项目"}, SimpleNamespace(project_id="project-test", project_lock_version=1, version_id="version-test"),
            "请先完成配图", image_model={"shots": shots}, changed={},
        )
        self.assertEqual(result["decision"]["problem_count"], 2)
        self.assertEqual(result["prerequisite_groups"][0]["kind"], "missing_images")
        self.assertEqual(result["prerequisite_groups"][0]["count"], 2)
        self.assertEqual([item["shot_id"] for item in result["prerequisite_groups"][0]["items"]], ["shot-1", "shot-2"])
        self.assertFalse(result["ready_for_final"])

    def test_missing_recheck_and_machine_causes_remain_separate(self):
        service = object.__new__(FormalReviewWorkspaceService)
        result = service._blocked(
            {"name": "匿名项目"}, SimpleNamespace(project_id="project-test", project_lock_version=1, version_id="version-test"),
            "请先完成配图", image_model={"shots": [
                {"shot_no": "1", "shot_id": "shot-1", "confirmed": False, "selected_image_key": ""},
                {"shot_no": "2", "shot_id": "shot-2", "confirmed": True, "selected_image_key": "image-2", "image_reconfirmation_required": True},
            ]}, changed={}, machine={"blocking_items": ["匿名检查问题"]},
        )
        self.assertEqual({item["kind"] for item in result["prerequisite_groups"]}, {"missing_images", "image_recheck", "machine"})
        self.assertEqual(result["decision"]["problem_count"], 3)
        self.assertEqual(result["decision"]["blocking_count"], 1)
