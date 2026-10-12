from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from mvp_v2.run_contract import sha256_file
from mvp_v2.template_rules import (
    TEMPLATE_RULES_NAME,
    TemplateRuleError,
    builtin_template_rules,
    template_rule_contract,
    validate_template_rules,
)


class TemplateRulesTest(unittest.TestCase):
    def test_all_supported_categories_have_contiguous_structural_slots(self):
        expected_counts = {
            "launch": 17,
            "exhibition": 19,
            "annual": 25,
            "sports": 22,
            "music_festival": 18,
            "star_activity": 18,
        }
        for category_id, expected_count in expected_counts.items():
            payload = builtin_template_rules(category_id)
            structural = [
                rule for rule in payload["rules"]
                if rule["level"] == "structural_required"
            ]
            slots = {
                slot
                for rule in structural
                for slot in range(
                    rule["selector"]["slot_start"],
                    rule["selector"]["slot_end"] + 1,
                )
            }
            self.assertEqual(slots, set(range(1, expected_count + 1)))

    def test_star_activity_first_arc_has_explicit_roles(self):
        payload = builtin_template_rules("star_activity")
        first = payload["rules"][0]
        self.assertEqual(first["selector"]["slot_start"], 1)
        self.assertEqual(first["selector"]["slot_end"], 3)
        self.assertEqual(first["level"], "structural_required")
        self.assertEqual(first["allowed_event_roles"], ["atmosphere", "space", "arrival"])

    def test_duplicate_rule_field_is_rejected(self):
        payload = builtin_template_rules("star_activity")
        payload["rules"].append(dict(payload["rules"][0], rule_id="duplicate"))
        with self.assertRaisesRegex(TemplateRuleError, "重复覆盖"):
            validate_template_rules(payload)

    def test_legacy_package_uses_versioned_default_with_warning(self):
        contract = template_rule_contract({
            "schema_version": "mplus-asset-package/v2",
            "category_id": "star_activity",
            "package_root": Path("/not/read"),
        })
        self.assertEqual(contract["status"], "legacy_default")
        self.assertIn("template_rules_legacy_default", contract["warning_codes"])
        self.assertTrue(contract["sha256"])

    def test_v3_package_binds_rule_file_sha(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rule_path = root / TEMPLATE_RULES_NAME
            rule_path.write_text(
                json.dumps(builtin_template_rules("star_activity"), ensure_ascii=False),
                encoding="utf-8",
            )
            package = {
                "schema_version": "mplus-asset-package/v3",
                "category_id": "star_activity",
                "package_root": root,
                "template_rules": TEMPLATE_RULES_NAME,
                "template_rules_sha256": sha256_file(rule_path),
            }
            self.assertEqual(template_rule_contract(package)["status"], "ready")
            package["template_rules_sha256"] = "0" * 64
            with self.assertRaisesRegex(TemplateRuleError, "SHA"):
                template_rule_contract(package)


if __name__ == "__main__":
    unittest.main()
