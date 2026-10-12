from urllib.parse import parse_qs, urlsplit
import unittest

from mvp_v2.cloud_runtime import CloudRuntimeError
from mvp_v2.github_oauth import (
    GithubOAuthSettings,
    authorization_request,
    complete_authorization,
)


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class GithubClient:
    def post(self, url, **kwargs):
        self.token_request = (url, kwargs)
        return Response({"access_token": "secret-access-token"})

    def get(self, url, **kwargs):
        if url.endswith("/user"):
            return Response({"id": 12345, "login": "Approved-User"})
        return Response([{"login": "approved-org"}])


class Repository:
    def create_web_session(self, **values):
        self.values = values


class GithubOAuthTests(unittest.TestCase):
    def setUp(self):
        self.settings = GithubOAuthSettings(
            client_id="client-id",
            client_secret="client-secret",
            public_url="https://mplus.example.com",
            session_secret="s" * 32,
            allowed_users=frozenset({"approved-user"}),
            allowed_orgs=frozenset({"approved-org"}),
        )

    def test_web_flow_uses_state_pkce_and_persists_only_session_hash(self):
        url, cookie = authorization_request(self.settings)
        query = parse_qs(urlsplit(url).query)
        repository = Repository()

        session_token, identity = complete_authorization(
            self.settings,
            repository,
            code="temporary-code",
            state=query["state"][0],
            flow_cookie=cookie,
            client=GithubClient(),
        )

        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotEqual(repository.values["session_sha256"], session_token)
        self.assertEqual(len(repository.values["session_sha256"]), 64)
        self.assertEqual(identity["github_id"], 12345)

    def test_state_mismatch_is_rejected_before_token_exchange(self):
        _url, cookie = authorization_request(self.settings)
        with self.assertRaisesRegex(CloudRuntimeError, "state"):
            complete_authorization(
                self.settings,
                Repository(),
                code="temporary-code",
                state="wrong-state",
                flow_cookie=cookie,
                client=GithubClient(),
            )


if __name__ == "__main__":
    unittest.main()
