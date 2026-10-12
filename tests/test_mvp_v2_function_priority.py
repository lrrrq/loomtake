import unittest

from mvp_v2.image_retrieval import _expected_function_bonus


class FunctionPriorityTests(unittest.TestCase):
    def test_expected_slot_function_scores_above_allowed_adaptation(self):
        self.assertGreater(
            _expected_function_bonus(6, ("guest_group", "crowd_flow")),
            _expected_function_bonus(6, ("crowd_flow",)),
        )
        self.assertGreater(
            _expected_function_bonus(14, ("performance",)),
            _expected_function_bonus(14, ("audience_recording",)),
        )


if __name__ == "__main__":
    unittest.main()
