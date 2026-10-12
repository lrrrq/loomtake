"""Retry receipts refer to the version they produced even after CURRENT moves."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from mvp_v2.formal_agent import FormalExecutableAgentService


class RetryResultVersionTests(unittest.TestCase):
    def test_successful_receipt_binds_its_frozen_version_not_newer_current(self):
        versions = [
            {"version_id": "version-one", "project_id": "project", "run_id": "run-one",
             "parent_version_id": None, "artifact_ref": "artifact://run/run-one/workbook",
             "content_sha256": "1" * 64, "created_by_actor_id": "actor", "created_at": "2026-09-09T10:00:00Z"},
            {"version_id": "version-two", "project_id": "project", "run_id": "run-two",
             "parent_version_id": "version-one", "artifact_ref": "artifact://run/run-two/workbook",
             "content_sha256": "2" * 64, "created_by_actor_id": "actor", "created_at": "2026-09-09T11:00:00Z"},
        ]
        projects = Mock()
        projects.get_project.return_value = {"project_id": "project", "current_version_id": "version-two"}
        projects.list_project_versions.return_value = versions
        projects.get_project_version.side_effect = lambda _auth, version_id: next(item for item in versions if item["version_id"] == version_id)
        artifacts = Mock()
        artifacts.resolve_artifact.side_effect = lambda run_id, _name: SimpleNamespace(
            ref=next(item["artifact_ref"] for item in versions if item["run_id"] == run_id),
            sha256=next(item["content_sha256"] for item in versions if item["run_id"] == run_id),
        )
        agent = object.__new__(FormalExecutableAgentService)
        agent.services = SimpleNamespace(projects=projects, artifacts=artifacts)
        result = agent._stage_retry_response("anonymous-session", "project", {
            "type": "formal_stage_retry_attempt_created", "status": "accepted",
            "new_attempt_id": "attempt-one", "execution": {
                "terminal_status": "succeeded", "stage_id": "formal_generation_plan", "output_ref": versions[0]["artifact_ref"],
                "output_sha256": versions[0]["content_sha256"], "replayed": True,
            },
        })
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result_version"], "version-one")
        self.assertEqual(result["execution"]["output_ref"], versions[0]["artifact_ref"])


if __name__ == "__main__":
    unittest.main()
