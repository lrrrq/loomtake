from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
import secrets
from urllib.parse import urlencode

import httpx

from .cloud_runtime import CloudRuntimeError, PostgresCloudRunRepository


FLOW_COOKIE = "mplus_github_oauth_flow"
SESSION_COOKIE = "mplus_github_session"


@dataclass(frozen=True)
class GithubOAuthSettings:
    client_id: str
    client_secret: str
    public_url: str
    session_secret: str
    allowed_users: frozenset[str]
    allowed_orgs: frozenset[str]

    @classmethod
    def from_environment(cls, environ: dict | None = None):
        values = os.environ if environ is None else environ
        users = _csv(values.get("MPLUS_GITHUB_ALLOW_USERS"))
        orgs = _csv(values.get("MPLUS_GITHUB_ALLOW_ORGS"))
        settings = cls(
            client_id=_required(values.get("MPLUS_GITHUB_CLIENT_ID"), "MPLUS_GITHUB_CLIENT_ID"),
            client_secret=_required(values.get("MPLUS_GITHUB_CLIENT_SECRET"), "MPLUS_GITHUB_CLIENT_SECRET"),
            public_url=_required(values.get("MPLUS_PUBLIC_URL"), "MPLUS_PUBLIC_URL").rstrip("/"),
            session_secret=_required(values.get("MPLUS_GITHUB_SESSION_SECRET"), "MPLUS_GITHUB_SESSION_SECRET"),
            allowed_users=frozenset(users),
            allowed_orgs=frozenset(orgs),
        )
        if len(settings.session_secret) < 32:
            raise CloudRuntimeError("MPLUS_GITHUB_SESSION_SECRET 不得少于 32 字符")
        if not settings.allowed_users and not settings.allowed_orgs:
            raise CloudRuntimeError("GitHub OAuth 必须配置账号或组织允许名单")
        return settings

    @property
    def callback_url(self) -> str:
        return f"{self.public_url}/auth/github/callback"


def authorization_request(settings: GithubOAuthSettings) -> tuple[str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("utf-8")).digest()
    ).decode("ascii").rstrip("=")
    flow_cookie = _sign_payload({
        "state": state,
        "verifier": verifier,
        "expires_at": int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp()),
    }, settings.session_secret)
    query = urlencode({
        "client_id": settings.client_id,
        "redirect_uri": settings.callback_url,
        "scope": "read:user read:org",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "allow_signup": "false",
    })
    return f"https://github.com/login/oauth/authorize?{query}", flow_cookie


def complete_authorization(
    settings: GithubOAuthSettings,
    repository: PostgresCloudRunRepository,
    *,
    code: str,
    state: str,
    flow_cookie: str,
    client=httpx,
) -> tuple[str, dict]:
    flow = _verify_payload(flow_cookie, settings.session_secret)
    if not hmac.compare_digest(str(flow.get("state") or ""), str(state or "")):
        raise CloudRuntimeError("GitHub OAuth state 不匹配")
    if int(flow.get("expires_at") or 0) < int(datetime.now(timezone.utc).timestamp()):
        raise CloudRuntimeError("GitHub OAuth state 已过期")
    token_response = client.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "code": _required(code, "OAuth code"),
            "redirect_uri": settings.callback_url,
            "code_verifier": _required(flow.get("verifier"), "OAuth code verifier"),
        },
        timeout=20,
    )
    token_response.raise_for_status()
    access_token = str(token_response.json().get("access_token") or "")
    if not access_token:
        raise CloudRuntimeError("GitHub OAuth 未返回 access token")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {access_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    user_response = client.get("https://api.github.com/user", headers=headers, timeout=20)
    user_response.raise_for_status()
    user = user_response.json()
    github_id = user.get("id")
    login = str(user.get("login") or "").strip()
    if type(github_id) is not int or github_id < 1 or not login:
        raise CloudRuntimeError("GitHub 用户身份无效")
    organizations = []
    if settings.allowed_orgs:
        org_response = client.get(
            "https://api.github.com/user/orgs?per_page=100",
            headers=headers,
            timeout=20,
        )
        org_response.raise_for_status()
        organizations = [
            str(item.get("login") or "").strip().casefold()
            for item in org_response.json()
            if str(item.get("login") or "").strip()
        ]
    allowed = (
        login.casefold() in settings.allowed_users
        or bool(set(organizations) & set(settings.allowed_orgs))
    )
    if not allowed:
        raise CloudRuntimeError("GitHub 账号或组织不在允许名单")
    session_token = secrets.token_urlsafe(48)
    session_sha = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=8)
    repository.create_web_session(
        github_id=github_id,
        github_login=login,
        organizations=organizations,
        session_sha256=session_sha,
        expires_at=expires_at,
    )
    return session_token, {
        "github_id": github_id,
        "github_login": login,
        "organizations": organizations,
        "expires_at": expires_at,
    }


def session_sha256(session_token: str) -> str:
    return hashlib.sha256(_required(session_token, "session token").encode("utf-8")).hexdigest()


def _sign_payload(payload: dict, secret: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _verify_payload(value: str, secret: str) -> dict:
    try:
        body, signature = value.rsplit(".", 1)
        expected = hmac.new(
            secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise CloudRuntimeError("GitHub OAuth flow cookie 无效") from exc
    if not isinstance(payload, dict):
        raise CloudRuntimeError("GitHub OAuth flow cookie 无效")
    return payload


def _csv(value: object) -> set[str]:
    return {item.strip().casefold() for item in str(value or "").split(",") if item.strip()}


def _required(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CloudRuntimeError(f"{field} 不得为空")
    return text
