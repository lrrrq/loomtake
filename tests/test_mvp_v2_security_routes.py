from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mvp_v2 import __version__, local_ui
from mvp_v2.cloud_runtime import MemoryCloudRunRepository
from mvp_v2.github_oauth import GithubOAuthSettings, session_sha256
from mvp_v2.readonly_agent import ReadonlyAgentService
from mvp_v2.runtime_config import load_runtime_config
from tests.test_mvp_v2_cloud_runtime import _package


class SecurityRouteTests(unittest.TestCase):
    def test_local_ui_token_is_uncached_and_rotated_token_cannot_dispatch_mutation(self):
        owner, project = self._owner_and_project()
        client = self._client()
        session = client.get("/api/ui/session")
        self.assertEqual(session.headers["cache-control"], "no-store")
        old_token = session.json()["token"]
        client.app.state.session_token = "anonymous-rotated-token"
        endpoint = "/api/agent/sessions"
        headers = {"X-MPlus-Agent-Auth": owner["auth_token"], "X-MPlus-UI-Token": old_token}
        rejected = client.post(endpoint, headers=headers, json={"project_id": project["project_id"]})
        self.assertEqual(rejected.status_code, 403)
        with self.api.services.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0], 0)
        headers["X-MPlus-UI-Token"] = client.get("/api/ui/session").json()["token"]
        self.assertEqual(client.post(endpoint, headers=headers, json={"project_id": project["project_id"]}).status_code, 200)

    def setUp(self):
        self.contexts = ExitStack()
        temporary = tempfile.TemporaryDirectory(prefix="mplus-security-routes-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(self.contexts.close)
        self.root = Path(temporary.name)
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("MPLUS_")
        }
        environment.update({
            "MPLUS_CONFIG": str(self.root / "absent.json"),
            "MPLUS_DATA_ROOT": str(self.root / "data"),
            "MPLUS_SOURCE_COMMIT": "a" * 40,
            "MPLUS_CONTAINER_IMAGE_DIGEST": "sha256:" + "b" * 64,
        })
        self.contexts.enter_context(patch.dict(os.environ, environment, clear=True))
        self.contexts.enter_context(patch.multiple(
            local_ui, AUTH_MODE="local", HOST="127.0.0.1",
            PUBLIC_URL="http://127.0.0.1:8765",
        ))
        self.api = ReadonlyAgentService.from_runtime_config(load_runtime_config())
        self.repository = MemoryCloudRunRepository()
        self.repository.register_package(_package(4, "a"))

    def _owner_and_project(self):
        owner = self.api.enable_local_owner(
            workspace_name="Anonymous", display_name="Owner", confirmed=True,
        )
        project = self.api.services.projects.create_project(
            owner["auth_token"], workspace_id=owner["workspace_id"],
            name="Anonymous launch", category_id="launch", expected_workspace_version=0,
        )
        return owner, project

    def _actor_token(self, owner, *, role, project_id=None):
        workspace = self.api.services.projects.get_workspace(
            owner["auth_token"], owner["workspace_id"],
        )
        actor = self.api.services.identity.create_actor(
            owner["auth_token"], workspace_id=owner["workspace_id"],
            display_name="Anonymous colleague", auth_source="local-test", role=role,
            project_id=project_id,
            expected_workspace_version=workspace["workspace_version"],
        )
        return self.api.services.identity.issue_auth_session(actor_id=actor["actor_id"])["auth_token"]

    def _payload(self, project):
        return {
            "project_id": project["project_id"], "category_id": "launch",
            "package_ref": "launch-core@4",
            "input_ref": "cos://private-123/input?sha256=" + "c" * 64,
            "input_sha256": "c" * 64, "input_filename": "brief.txt",
            "runner_version": __version__, "idempotency_key": "anonymous-cloud-request-1",
            "request": {
                "ai_provider": "deepseek-http", "ai_model": "deepseek-chat",
                "prompt_version": "v1", "prompt_schema_version": "v1",
            },
        }

    def _client(self):
        return self.contexts.enter_context(TestClient(local_ui.create_app(
            agent_api=self.api, cloud_run_repository=self.repository,
        )))

    def test_viewer_can_read_cloud_run_but_cannot_generate_or_cancel(self):
        owner, project = self._owner_and_project()
        viewer = self._actor_token(owner, role="viewer")
        client = self._client()
        url = f"/api/formal/workspaces/{owner['workspace_id']}/cloud-runs"
        created = client.post(url, json=self._payload(project), headers={"X-MPlus-Agent-Auth": owner["auth_token"]})
        self.assertEqual(created.status_code, 200)
        run_url = url + "/" + created.json()["run_id"]
        headers = {"X-MPlus-Agent-Auth": viewer}
        self.assertEqual(client.get(run_url, headers=headers).status_code, 200)
        for action, target, payload in (
            ("generate", url, {**self._payload(project), "idempotency_key": "viewer-new-request"}),
            ("cancel", run_url + "/cancel", None),
        ):
            with self.subTest(action=action):
                with patch.object(self.repository, "create_run", wraps=self.repository.create_run) as create, \
                        patch.object(self.repository, "cancel", wraps=self.repository.cancel) as cancel:
                    response = client.post(target, json=payload, headers=headers)
                    create.assert_not_called()
                    cancel.assert_not_called()
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["detail"]["code"], "PERMISSION_DENIED")
        self.assertEqual(self.repository.get_run(created.json()["run_id"])["status"], "queued")

    def test_project_editor_can_generate_and_cancel_in_their_scope(self):
        owner, project = self._owner_and_project()
        editor = self._actor_token(owner, role="editor", project_id=project["project_id"])
        client = self._client()
        url = f"/api/formal/workspaces/{owner['workspace_id']}/cloud-runs"
        headers = {"X-MPlus-Agent-Auth": editor}
        created = client.post(url, json=self._payload(project), headers=headers)
        self.assertEqual(created.status_code, 200)
        cancelled = client.post(url + "/" + created.json()["run_id"] + "/cancel", headers=headers)
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")

    def test_project_editor_cannot_write_another_projects_cloud_runs(self):
        owner, project = self._owner_and_project()
        other = self.api.services.projects.create_project(
            owner["auth_token"], workspace_id=owner["workspace_id"],
            name="Another anonymous launch", category_id="launch", expected_workspace_version=1,
        )
        editor = self._actor_token(owner, role="editor", project_id=project["project_id"])
        client = self._client()
        url = f"/api/formal/workspaces/{owner['workspace_id']}/cloud-runs"
        payload = self._payload(other)
        run = client.post(url, json=payload, headers={"X-MPlus-Agent-Auth": owner["auth_token"]})
        self.assertEqual(run.status_code, 200)
        headers = {"X-MPlus-Agent-Auth": editor}
        for endpoint, body in ((url, payload), (url + "/" + run.json()["run_id"] + "/cancel", None)):
            with self.subTest(endpoint=endpoint):
                denied = client.post(endpoint, json=body, headers=headers)
                self.assertEqual(denied.status_code, 403)
                self.assertEqual(denied.json()["detail"]["code"], "PERMISSION_DENIED")
        self.assertEqual(self.repository.get_run(run.json()["run_id"])["status"], "queued")

    def test_github_session_cannot_bootstrap_or_reissue_a_local_owner(self):
        settings = GithubOAuthSettings(
            client_id="anonymous", client_secret="anonymous", public_url="https://testserver",
            session_secret="s" * 32, allowed_users=frozenset({"allowed-member"}),
            allowed_orgs=frozenset(),
        )
        oauth = SimpleNamespace(
            check_schema=lambda: None,
            validate_web_session=lambda digest: {
                "github_id": 123, "github_login": "allowed-member",
            } if digest == session_sha256("anonymous-cookie") else None,
        )
        # GitHub can also run behind a loopback reverse proxy; it must never issue local owners.
        with patch.object(local_ui, "AUTH_MODE", "github"), \
                patch("mvp_v2.github_oauth.GithubOAuthSettings.from_environment", return_value=settings), \
                patch("mvp_v2.cloud_runtime.PostgresCloudRunRepository.from_environment", return_value=oauth):
            client = self._client()
            client.cookies.set("mplus_github_session", "anonymous-cookie")
            ui_token = client.get("/api/ui/session").json()["token"]
            for endpoint in ("/api/formal/owner", "/api/agent/owner/enable", "/api/agent/owner/reissue"):
                with self.subTest(endpoint=endpoint):
                    response = client.post(
                        endpoint, headers={"X-MPlus-UI-Token": ui_token},
                        json={"workspace_name": "Anonymous", "display_name": "Allowed member", "confirmed": True},
                    )
                    self.assertEqual(response.status_code, 409)
                    self.assertNotIn("auth_token", response.json())
            with self.api.services.store.connection() as connection:
                for table in ("workspaces", "actors", "auth_sessions"):
                    self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            # Existing local credentials may be migrated, but OAuth must not mint new ones.
            self._owner_and_project()
            denied = client.post(
                "/api/agent/owner/reissue", json={"confirmed": True},
                headers={"X-MPlus-UI-Token": ui_token},
            )
            self.assertEqual(denied.status_code, 409)
            with self.api.services.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 1)

    def test_local_bootstrap_and_reissue_remain_available(self):
        client = self._client()
        headers = {"X-MPlus-UI-Token": client.get("/api/ui/session").json()["token"]}
        payload = {"workspace_name": "Anonymous", "display_name": "Owner", "confirmed": True}
        owner = client.post("/api/agent/owner/enable", json=payload, headers=headers)
        self.assertEqual(owner.status_code, 200)
        self.assertEqual(client.post("/api/agent/owner/enable", json=payload, headers=headers).status_code, 409)
        reissued = client.post("/api/agent/owner/reissue", json={"confirmed": True}, headers=headers)
        self.assertEqual(reissued.status_code, 200)
        self.assertEqual(reissued.json()["workspace_id"], owner.json()["workspace_id"])


if __name__ == "__main__":
    unittest.main()
