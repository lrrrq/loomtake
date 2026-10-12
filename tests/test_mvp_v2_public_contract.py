import unittest

from mvp_v2.public_contract import (
    PUBLIC_CONTRACT_VERSION,
    PublicLifecycleStatus,
    STABLE_ENTITY_CONTRACTS,
    require_artifact_ref,
)


class PublicContractTests(unittest.TestCase):
    def test_v1_contract_freezes_statuses_and_entity_relationships(self):
        self.assertEqual(PUBLIC_CONTRACT_VERSION, "mplus-public-contract/v1")
        self.assertEqual(
            tuple(status.value for status in PublicLifecycleStatus),
            (
                "running",
                "blocked",
                "failed",
                "waiting_human_review",
                "pending_review",
                "completed",
                "published",
            ),
        )
        self.assertEqual(
            set(STABLE_ENTITY_CONTRACTS),
            {"brief", "plan", "script", "image", "task", "review", "publication"},
        )
        self.assertEqual(STABLE_ENTITY_CONTRACTS["review"]["parent"], "version_id")
        self.assertEqual(STABLE_ENTITY_CONTRACTS["publication"]["parent"], "version_id")

    def test_artifact_refs_are_logical_and_not_filesystem_paths(self):
        self.assertEqual(
            require_artifact_ref("artifact://project/p001/version/v001", "artifact_ref"),
            "artifact://project/p001/version/v001",
        )
        with self.assertRaises(ValueError):
            require_artifact_ref("/tmp/output.xlsx", "artifact_ref")


if __name__ == "__main__":
    unittest.main()
