import unittest

from mvp_v2.quality_contract import (
    HUMAN_PENDING,
    HUMAN_QUALITY_FAIL,
    HUMAN_QUALITY_PASS,
    MACHINE_FAIL,
    MACHINE_PASS,
    RELEASE_BLOCKED,
    RELEASE_PASS,
    RELEASE_TRIAL_PASS,
    QualityThresholds,
    compute_human_review_status,
    compute_release_decision,
    compute_review_metrics,
    labor_time_saving_rate,
    release_exit_code,
)


REVIEW_COLUMNS = ("结构审核", "叙事审核", "节奏审核", "创意审核", "配图审核", "总体审核")


def review_row(slot_index, overall="通过", image="通过", major_change=False, complete=True):
    row = {
        "slot_index": slot_index,
        "重大修改": major_change,
        "总体审核": overall,
        "配图审核": image,
    }
    for column in REVIEW_COLUMNS:
        row.setdefault(column, overall)
    if not complete:
        for column in REVIEW_COLUMNS:
            row[column] = ""
    return row


class QualityContractTests(unittest.TestCase):
    def test_default_eighteen_shot_profile_and_metrics(self):
        thresholds = QualityThresholds()
        rows = [review_row(slot) for slot in range(1, 16)]
        rows.extend(review_row(slot, overall="需修改") for slot in range(16, 19))
        rows[0]["重大修改"] = True
        rows[-1]["配图审核"] = "需修改"

        metrics = compute_review_metrics(rows)

        self.assertEqual(thresholds.shot_count, 18)
        self.assertEqual(thresholds.required_key_slots, (1, 3, 10, 11, 12, 15, 16, 18))
        self.assertEqual(metrics["total_shot_count"], 18)
        self.assertEqual(metrics["approved_shot_count"], 15)
        self.assertEqual(metrics["needs_revision_shot_count"], 3)
        self.assertEqual(metrics["rejected_shot_count"], 0)
        self.assertEqual(metrics["major_change_count"], 1)
        self.assertEqual(metrics["accepted_image_count"], 17)
        self.assertEqual(metrics["shot_pass_rate"], 15 / 18)
        self.assertEqual(metrics["image_acceptance"], 17 / 18)
        self.assertEqual(metrics["discussability_rate"], 17 / 18)

    def test_completed_quality_gate_passes_at_all_single_project_boundaries(self):
        rows = [review_row(slot) for slot in range(1, 19)]
        for slot in (2, 4, 5):
            rows[slot - 1] = review_row(slot, overall="需修改")
        rows[1]["重大修改"] = True
        rows[4]["配图审核"] = "需修改"

        metrics = compute_review_metrics(rows)

        self.assertEqual(metrics["failed_key_slots"], [])
        self.assertEqual(compute_human_review_status(metrics), HUMAN_QUALITY_PASS)

    def test_completed_rejected_reviews_fail_quality_gate(self):
        metrics = compute_review_metrics([
            review_row(slot, overall="不通过", image="不通过") for slot in range(1, 19)
        ])

        self.assertEqual(compute_human_review_status(metrics), HUMAN_QUALITY_FAIL)

    def test_failed_required_key_slot_cannot_pass_quality_gate(self):
        rows = [review_row(slot) for slot in range(1, 19)]
        for slot in (1, 4, 5):
            rows[slot - 1] = review_row(slot, overall="需修改")

        metrics = compute_review_metrics(rows)

        self.assertEqual(metrics["failed_key_slots"], [1])
        self.assertEqual(compute_human_review_status(metrics), HUMAN_QUALITY_FAIL)

    def test_duplicate_slots_fail_without_assuming_a_fixed_template_count(self):
        rows = [review_row(slot) for slot in range(1, 16)]
        rows.extend(review_row(slot) for slot in (2, 4, 5))

        metrics = compute_review_metrics(rows)

        self.assertEqual(metrics["failed_key_slots"], [])
        self.assertEqual(compute_human_review_status(metrics), HUMAN_QUALITY_FAIL)

    def test_custom_required_key_slots_are_used_by_human_quality_gate(self):
        rows = [review_row(slot) for slot in range(1, 19)]
        rows[1] = review_row(2, overall="需修改")
        metrics = compute_review_metrics(rows)

        self.assertEqual(compute_human_review_status(metrics), HUMAN_QUALITY_PASS)
        self.assertEqual(
            compute_human_review_status(
                metrics,
                QualityThresholds(required_key_slots=(2,)),
            ),
            HUMAN_QUALITY_FAIL,
        )

    def test_duplicate_slot_and_missing_slot_cannot_fake_complete_18_shots(self):
        rows = [review_row(slot) for slot in range(1, 19)]
        rows[8] = review_row(8)

        self.assertEqual(
            compute_human_review_status(compute_review_metrics(rows)),
            HUMAN_QUALITY_FAIL,
        )

    def test_out_of_range_slot_and_missing_slot_cannot_fake_complete_18_shots(self):
        rows = [review_row(slot) for slot in range(1, 19)]
        rows[8] = review_row(19)

        self.assertEqual(
            compute_human_review_status(compute_review_metrics(rows)),
            HUMAN_QUALITY_FAIL,
        )

    def test_blank_overall_uses_other_decision_but_invalid_status_is_not_discussable(self):
        rows = [review_row(slot) for slot in range(1, 19)]
        rows[0]["总体审核"] = ""
        rows[1]["总体审核"] = "待定"

        metrics = compute_review_metrics(rows)

        self.assertEqual(metrics["discussability_rate"], 17 / 18)

    def test_incomplete_reviews_are_pending(self):
        rows = [review_row(slot) for slot in range(1, 18)]
        rows.append(review_row(18, complete=False))

        self.assertEqual(compute_human_review_status(compute_review_metrics(rows)), HUMAN_PENDING)
        self.assertEqual(compute_human_review_status(compute_review_metrics(rows[:-1])), HUMAN_QUALITY_PASS)

    def test_labor_time_saving_rate_is_bounded_and_rejects_nonpositive_baseline(self):
        self.assertEqual(labor_time_saving_rate(100, -100), 1)
        self.assertEqual(labor_time_saving_rate(100, 300), -1)
        self.assertEqual(labor_time_saving_rate(100, 70), 0.3)
        with self.assertRaises(ValueError):
            labor_time_saving_rate(0, 10)

    def test_release_decision_and_exit_codes_keep_pending_blocked(self):
        thresholds = QualityThresholds()
        kwargs = {
            "machine_status": MACHINE_PASS,
            "warning_count": 0,
            "discussability_rate": thresholds.min_discussability_rate,
            "labor_saving_rate": thresholds.min_labor_time_saving_rate,
        }

        self.assertEqual(
            compute_release_decision(human_status=HUMAN_QUALITY_PASS, **kwargs),
            RELEASE_PASS,
        )
        self.assertEqual(
            compute_release_decision(human_status=HUMAN_QUALITY_FAIL, **kwargs),
            RELEASE_TRIAL_PASS,
        )
        self.assertEqual(
            compute_release_decision(human_status=HUMAN_PENDING, **kwargs),
            RELEASE_BLOCKED,
        )
        self.assertEqual(release_exit_code(human_status=HUMAN_QUALITY_PASS, **kwargs), 0)
        self.assertEqual(release_exit_code(human_status=HUMAN_QUALITY_FAIL, **kwargs), 4)
        self.assertEqual(release_exit_code(human_status=HUMAN_PENDING, **kwargs), 3)
        self.assertEqual(release_exit_code(machine_status=MACHINE_FAIL, human_status=HUMAN_QUALITY_PASS, warning_count=0, discussability_rate=1, labor_saving_rate=1), 2)
        self.assertEqual(release_exit_code(human_status=HUMAN_QUALITY_PASS, system_error=True, **kwargs), 1)


if __name__ == "__main__":
    unittest.main()
