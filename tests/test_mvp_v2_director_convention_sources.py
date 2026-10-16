"""Public convention provenance keeps immutable receipts and private dialogue separate."""
from copy import deepcopy
import json
import unittest

from mvp_v2.identity_service import LocalIdentityAdapter
from tests import test_mvp_v2_creative_workbench as workbench_fixture


class DirectorConventionSourceTests(unittest.TestCase):
    def setUp(self):
        self.base = workbench_fixture.CreativeWorkbenchTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.agent, self.token, self.scope = self.base.agent, self.base.token, self.base.scope
        self.session_id = self.base.session["agent_session_id"]
        self.statement = "开场保留真实现场交流，减少口号式旁白"

    def _version(self):
        return self.agent.project_knowledge.get_state(
            self.token, self.scope["workspace_id"], project_id=self.scope["project_id"],
        )["knowledge_version"]

    def _message(self):
        started = self.agent.start_workbench(self.token, self.session_id,
            content="私人讨论原话：先谈备用方案，再明确开场的声音约定。",
            context=self.base.context(), operation_key="convention-source-message")
        return started["task"]["task_id"] + ":user"

    def _save(self, message_id=""):
        return self.agent.save_project_convention(self.token, self.session_id,
            statement=self.statement, expected_knowledge_version=self._version(),
            confirmed=True, source_message_id=message_id)

    def _decision(self, *, agent=None, token=None, session_id=None, context=False):
        agent = agent or self.agent
        method = agent.director_context if context else agent.get_session
        value = method(token or self.token, session_id or self.session_id)
        return value["project_decisions"][0]

    def test_own_source_survives_new_session_restart_and_preserves_raw_evidence(self):
        message_id = self._message()
        saved = self._save(message_id)
        raw = deepcopy(self.agent.project_knowledge.list_project_conventions(self.token,
            self.scope["project_id"], include_inactive=True))
        new_session = self.agent.services.tasks.create_agent_session(self.token,
            project_id=self.scope["project_id"])["agent_session_id"]
        database = self.base.database()
        fresh = self.base.new_agent()
        projected = self._decision(agent=fresh, session_id=new_session)
        expected = [{"kind": "message", "label": "用户从会话消息明确保存",
            "agent_session_id": self.session_id, "message_id": message_id}]
        self.assertEqual(projected["public_sources"], expected)
        self.assertEqual(projected["evidence_refs"], saved["evidence_refs"])
        self.assertEqual(self._decision(agent=fresh, session_id=new_session, context=True)["public_sources"], expected)
        self.assertNotIn("私人讨论原话", json.dumps(projected, ensure_ascii=False))
        self.assertEqual(self.agent.project_knowledge.list_project_conventions(self.token,
            self.scope["project_id"], include_inactive=True), raw)
        self.assertEqual(self.base.database(), database)
        self.assertEqual(self.base.business(), self.base.initial)

    def test_manual_confirmation_has_explainable_source_without_message_link(self):
        saved = self._save()
        decision = self._decision(context=True)
        self.assertEqual(decision["public_sources"], [{"kind": "confirmation", "label": "用户明确保存"}])
        self.assertEqual(decision["evidence_refs"], saved["evidence_refs"])

    def test_shared_convention_does_not_expose_another_actor_private_message_or_session(self):
        message_id = self._message()
        saved = self._save(message_id)
        identity = LocalIdentityAdapter(self.agent.services.store)
        workspace = self.agent.services.projects.get_workspace(self.token, self.scope["workspace_id"])
        actor = identity.create_actor(self.token, **self.scope, display_name="匿名协作编辑",
            auth_source="local", role="editor", expected_workspace_version=workspace["workspace_version"])
        token = identity.issue_auth_session(actor_id=actor["actor_id"])["auth_token"]
        session_id = self.agent.services.tasks.create_agent_session(token,
            project_id=self.scope["project_id"])["agent_session_id"]
        projected = self._decision(token=token, session_id=session_id)
        self.assertEqual(projected["statement"], self.statement)
        self.assertEqual(projected["evidence_refs"], saved["evidence_refs"])
        self.assertEqual(projected["public_sources"], [{"kind": "confirmation", "label": "项目成员明确保存"}])
        public = json.dumps(projected, ensure_ascii=False)
        for private in ("私人讨论原话", self.session_id, message_id):
            self.assertNotIn(private, public)

    def test_wrong_project_or_tampered_receipt_never_produces_a_source_link(self):
        from mvp_v2.formal_brief_workspace_service import FormalBriefArtifactStore
        artifacts = FormalBriefArtifactStore(self.agent.script_revision.brief_adapter.service.artifacts.root.parent,
            namespace="project-convention")
        actor_id = self.agent.services.tasks.get_agent_session(self.token, self.session_id)["actor_id"]
        receipt = artifacts.write(json.dumps({"schema": "mplus-project-convention-source/v1",
            **self.scope, "project_id": "different-project", "actor_id": actor_id,
            "agent_session_id": self.session_id, "statement": self.statement,
            "source_message": {"message_id": "foreign-private-message"}, "explicit_confirmation": True}).encode())
        self.agent.project_knowledge.add_project_convention(self.token, **self.scope,
            statement=self.statement, evidence_refs=[receipt.ref], expected_knowledge_version=self._version(), confirmed=True)
        expected = [{"kind": "unavailable", "label": "来源凭据暂无法读取，约定记录保留"}]
        self.assertEqual(self._decision()["public_sources"], expected)
        target = artifacts.root / receipt.sha256[:2] / (receipt.sha256 + ".json")
        target.write_text('{"sensitive":"untrusted replacement"}')
        self.assertEqual(self._decision()["public_sources"], expected)
        self.assertEqual(self.base.business(), self.base.initial)


if __name__ == "__main__":
    unittest.main()
