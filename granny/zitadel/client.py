"""Thin wrapper around the Zitadel management/admin REST API.

Mirrors :mod:`granny.authentik.client`. Uses ``requests`` (a granny core
dependency); machine authentication uses the OIDC JWT-profile grant signed
with a Zitadel service-account key (``PyJWT`` + ``cryptography`` for RS256),
so granny never has to store a long-lived bearer.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Scope that asks Zitadel to mint a token whose audience includes the
# management/admin APIs of the instance (the reserved ``zitadel`` project).
_API_SCOPE = "openid urn:zitadel:iam:org:project:id:zitadel:aud"


class ZitadelError(RuntimeError):
    """Raised on non-2xx responses from the Zitadel API."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


def _jwt_profile_token(key: dict[str, Any], base_url: str, *, timeout: float) -> str:
    """Exchange a Zitadel service-account key for a bearer access token.

    ``key`` is the parsed ``iam-admin.json`` (fields: ``userId``, ``keyId``,
    ``key`` = PEM RSA private key). Builds an RS256-signed JWT assertion and
    POSTs it to the token endpoint with the JWT-profile grant.
    """
    try:
        import jwt  # PyJWT
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ZitadelError(
            0,
            "PyJWT is required for Zitadel service-account auth. "
            "Install it (it is a granny core dependency: `uv sync`).",
        ) from exc

    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": key["userId"],
            "sub": key["userId"],
            "aud": base_url,
            "iat": now,
            "exp": now + 3600,
        },
        key["key"],
        algorithm="RS256",
        headers={"kid": key["keyId"]},
    )
    resp = requests.post(
        base_url.rstrip("/") + "/oauth/v2/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
            "scope": _API_SCOPE,
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise ZitadelError(resp.status_code, resp.text)
    token = resp.json().get("access_token")
    if not token:
        raise ZitadelError(
            0, f"token endpoint returned no access_token: {resp.text[:200]}"
        )
    return token


class ZitadelClient:
    """Authenticated client for the Zitadel management + admin REST APIs.

    Construct directly with a bearer ``token`` (e.g. a PAT), or use
    :meth:`from_service_account` / :meth:`from_environment` to authenticate a
    machine user via the JWT-profile grant.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )

    @classmethod
    def from_service_account(
        cls, key: dict[str, Any], base_url: str, *, timeout: float = 30.0
    ) -> "ZitadelClient":
        """Build a client by exchanging a service-account key for a token."""
        token = _jwt_profile_token(key, base_url, timeout=timeout)
        return cls(base_url=base_url, token=token, timeout=timeout)

    @classmethod
    def from_environment(cls) -> "ZitadelClient":
        """Build a client from env vars + granny's secret pipeline.

        Base URL: ``ZITADEL_DOMAIN`` (e.g. ``https://id.pseekoo.io``) — this is
        plain config, NOT a secret, so set it via env / .env / .deploy.env; it
        is deliberately not vault-resolved.

        Credential resolution (first match wins):

        1. ``ZITADEL_API_TOKEN`` — a PAT, used directly as a bearer (env / vault
           at ``granny/infra/zitadel-api-token``).
        2. ``ZITADEL_SA_KEY`` — the service-account key JSON inline (env / vault
           at ``granny/infra/zitadel-sa-key``); JWT-profile exchanged.
        3. ``ZITADEL_SA_KEY_FILE`` — path to the service-account key JSON file.
        """
        from granny.credentials import get_secret

        # The IdP base URL is config, not a secret — env / .env / .deploy.env
        # only (deliberately not vault-resolved).
        base_url = os.environ.get("ZITADEL_DOMAIN")
        if not base_url:
            raise ZitadelError(
                0,
                "ZITADEL_DOMAIN is not set. It is config, not a secret — set it "
                "via env or .env/.deploy.env, e.g. "
                "ZITADEL_DOMAIN=https://id.pseekoo.io",
            )

        pat = os.environ.get("ZITADEL_API_TOKEN") or get_secret("ZITADEL_API_TOKEN")
        if pat:
            return cls(base_url=base_url, token=pat)

        key_json = os.environ.get("ZITADEL_SA_KEY") or get_secret("ZITADEL_SA_KEY")
        if not key_json:
            key_file = os.environ.get("ZITADEL_SA_KEY_FILE")
            if key_file:
                with open(key_file, encoding="utf-8") as fh:
                    key_json = fh.read()
        if not key_json:
            raise ZitadelError(
                0,
                "No Zitadel credential. Set ZITADEL_API_TOKEN (a PAT), or "
                "ZITADEL_SA_KEY / ZITADEL_SA_KEY_FILE (the service-account key JSON).",
            )
        return cls.from_service_account(json.loads(key_json), base_url)

    # ── HTTP plumbing ────────────────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute one API request and return the decoded JSON body."""
        if not path.startswith("/"):
            path = "/" + path
        resp = self._session.request(
            method=method.upper(),
            url=self.base_url + path,
            params=params or None,
            json=body,
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            raise ZitadelError(resp.status_code, resp.text)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ── Projects ─────────────────────────────────────────────────────────

    def find_project(self, name: str) -> dict[str, Any] | None:
        """Return the project dict whose name == ``name``, or None."""
        data = self.request(
            "POST",
            "/management/v1/projects/_search",
            body={
                "queries": [
                    {"nameQuery": {"name": name, "method": "TEXT_QUERY_METHOD_EQUALS"}}
                ]
            },
        )
        for proj in (data or {}).get("result", []) or []:
            if proj.get("name") == name:
                return proj
        return None

    def ensure_project(self, name: str) -> str:
        """Create the project if absent; return its id (idempotent)."""
        existing = self.find_project(name)
        if existing:
            return existing["id"]
        created = self.request("POST", "/management/v1/projects", body={"name": name})
        return created["id"]

    # ── OIDC applications ────────────────────────────────────────────────

    def find_oidc_app(self, project_id: str, name: str) -> dict[str, Any] | None:
        data = self.request(
            "POST",
            f"/management/v1/projects/{project_id}/apps/_search",
            body={
                "queries": [
                    {"nameQuery": {"name": name, "method": "TEXT_QUERY_METHOD_EQUALS"}}
                ]
            },
        )
        for app in (data or {}).get("result", []) or []:
            if app.get("name") == name:
                return app
        return None

    def create_oidc_app(
        self,
        project_id: str,
        name: str,
        redirect_uris: list[str],
        *,
        post_logout_uris: list[str] | None = None,
        confidential: bool = True,
        dev_mode: bool = False,
    ) -> dict[str, Any]:
        """Create an OIDC web app (auth-code + refresh, PKCE-capable).

        Returns the create response including ``clientId`` and, for confidential
        apps, ``clientSecret`` (only returned at creation time).
        """
        body = {
            "name": name,
            "redirectUris": redirect_uris,
            "postLogoutRedirectUris": post_logout_uris or [],
            "responseTypes": ["OIDC_RESPONSE_TYPE_CODE"],
            "grantTypes": [
                "OIDC_GRANT_TYPE_AUTHORIZATION_CODE",
                "OIDC_GRANT_TYPE_REFRESH_TOKEN",
            ],
            "appType": "OIDC_APP_TYPE_WEB",
            "authMethodType": (
                "OIDC_AUTH_METHOD_TYPE_BASIC"
                if confidential
                else "OIDC_AUTH_METHOD_TYPE_NONE"
            ),
            "version": "OIDC_VERSION_1_0",
            "devMode": dev_mode,
            "accessTokenType": "OIDC_TOKEN_TYPE_JWT",
        }
        return self.request(
            "POST", f"/management/v1/projects/{project_id}/apps/oidc", body=body
        )

    # ── Login policy / MFA ───────────────────────────────────────────────

    def get_login_policy(self) -> dict[str, Any]:
        """Instance default login policy."""
        return (self.request("GET", "/admin/v1/policies/login") or {}).get("policy", {})

    def enforce_mfa(
        self,
        *,
        passkey: bool = True,
        totp: bool = True,
        security_key: bool = True,
    ) -> dict[str, Any]:
        """Force MFA on the instance login policy and enable the chosen factors.

        - ``totp``           -> SECOND_FACTOR_TYPE_OTP (authenticator app)
        - ``security_key``   -> SECOND_FACTOR_TYPE_U2F (WebAuthn 2nd factor)
        - ``passkey``        -> MULTI_FACTOR_TYPE_U2F_WITH_VERIFICATION (passwordless)
        """
        cur = self.get_login_policy()
        # Update the base policy with forceMfa, preserving existing flags.
        policy_body = {
            "allowUsernamePassword": cur.get("allowUsernamePassword", True),
            "allowRegister": cur.get("allowRegister", False),
            "allowExternalIdp": cur.get("allowExternalIdp", True),
            "forceMfa": True,
            "passwordlessType": "PASSWORDLESS_TYPE_ALLOWED",
            "hidePasswordReset": cur.get("hidePasswordReset", False),
            "ignoreUnknownUsernames": cur.get("ignoreUnknownUsernames", False),
            "allowDomainDiscovery": cur.get("allowDomainDiscovery", True),
            "disableLoginWithEmail": cur.get("disableLoginWithEmail", False),
            "disableLoginWithPhone": cur.get("disableLoginWithPhone", False),
        }
        self.request("PUT", "/admin/v1/policies/login", body=policy_body)

        results: dict[str, Any] = {
            "forceMfa": True,
            "secondFactors": [],
            "multiFactors": [],
        }
        if totp:
            self._add_factor(
                "/admin/v1/policies/login/second_factors", "SECOND_FACTOR_TYPE_OTP"
            )
            results["secondFactors"].append("OTP")
        if security_key:
            self._add_factor(
                "/admin/v1/policies/login/second_factors", "SECOND_FACTOR_TYPE_U2F"
            )
            results["secondFactors"].append("U2F")
        if passkey:
            self._add_factor(
                "/admin/v1/policies/login/multi_factors",
                "MULTI_FACTOR_TYPE_U2F_WITH_VERIFICATION",
            )
            results["multiFactors"].append("U2F_WITH_VERIFICATION (passkey)")
        return results

    def _add_factor(self, path: str, factor_type: str) -> None:
        """Add a factor to the login policy; ignore 'already exists' (409)."""
        try:
            self.request("POST", path, body={"type": factor_type})
        except ZitadelError as exc:
            if exc.status != 409:
                raise

    # ── Projects / apps (list, get, rotate) ──────────────────────────────

    def list_projects(self) -> list[dict[str, Any]]:
        data = self.request(
            "POST", "/management/v1/projects/_search", body={"queries": []}
        )
        return (data or {}).get("result", []) or []

    def list_apps(self, project_id: str) -> list[dict[str, Any]]:
        data = self.request(
            "POST",
            f"/management/v1/projects/{project_id}/apps/_search",
            body={"queries": []},
        )
        return (data or {}).get("result", []) or []

    def get_app(self, project_id: str, app_id: str) -> dict[str, Any]:
        data = self.request(
            "GET", f"/management/v1/projects/{project_id}/apps/{app_id}"
        )
        return (data or {}).get("app", data or {})

    def rotate_app_secret(self, project_id: str, app_id: str) -> dict[str, Any]:
        """Regenerate an OIDC app's client secret. Returns ``{'clientSecret': ...}`` (shown once)."""
        return self.request(
            "POST",
            f"/management/v1/projects/{project_id}/apps/{app_id}/oidc_config/_generate_client_secret",
        )

    # ── Users ────────────────────────────────────────────────────────────

    def find_user(self, username: str) -> dict[str, Any] | None:
        """Find a human user by ``userName`` (exact), or None."""
        data = self.request(
            "POST",
            "/management/v1/users/_search",
            body={
                "queries": [
                    {
                        "userNameQuery": {
                            "userName": username,
                            "method": "TEXT_QUERY_METHOD_EQUALS",
                        }
                    }
                ]
            },
        )
        for user in (data or {}).get("result", []) or []:
            if user.get("userName") == username:
                return user
        return None

    def list_users(self) -> list[dict[str, Any]]:
        data = self.request(
            "POST", "/management/v1/users/_search", body={"queries": []}
        )
        return (data or {}).get("result", []) or []

    def create_user(
        self,
        username: str,
        *,
        email: str | None = None,
        first_name: str = "",
        last_name: str = "",
        display_name: str | None = None,
        password: str | None = None,
        email_verified: bool = False,
        password_change_required: bool = False,
    ) -> dict[str, Any]:
        """Create a human user (idempotent on ``userName``).

        Returns the existing user dict unchanged if ``username`` already exists,
        else the create response (carries ``userId``).
        """
        existing = self.find_user(username)
        if existing:
            return existing
        body: dict[str, Any] = {
            "userName": username,
            "profile": {
                "firstName": first_name or username,
                "lastName": last_name or username,
            },
        }
        if display_name:
            body["profile"]["displayName"] = display_name
        if email:
            body["email"] = {"email": email, "isEmailVerified": email_verified}
        if password:
            body["password"] = password
            body["passwordChangeRequired"] = password_change_required
        return self.request("POST", "/management/v1/users/human/_import", body=body)

    def set_user_password(
        self, user_id: str, password: str, *, change_required: bool = False
    ) -> None:
        """Admin-set a user's password (no email round-trip)."""
        self.request(
            "POST",
            f"/management/v1/users/{user_id}/password",
            body={
                "newPassword": {"password": password, "changeRequired": change_required}
            },
        )

    def delete_user(self, user_id: str) -> None:
        """Delete a user by id."""
        self.request("DELETE", f"/management/v1/users/{user_id}")

    # ── Personal access tokens (machine-user bearer tokens) ───────────────
    # A PAT is a long-lived bearer for a machine user — what other projects
    # store at vault ``granny/infra/zitadel-api-token`` so granny authenticates
    # without the service-account key. The token value is returned ONCE.

    def list_pats(self, user_id: str) -> list[dict[str, Any]]:
        data = self.request(
            "POST", f"/management/v1/users/{user_id}/pats/_search", body={"queries": []}
        )
        return (data or {}).get("result", []) or []

    def create_pat(self, user_id: str, *, expiration_date: str | None = None) -> dict[str, Any]:
        """Create a PAT for ``user_id``. Returns {tokenId, token} (token shown once)."""
        body: dict[str, Any] = {}
        if expiration_date:
            body["expirationDate"] = expiration_date
        return self.request("POST", f"/management/v1/users/{user_id}/pats", body=body)

    def delete_pat(self, user_id: str, token_id: str) -> None:
        """Revoke a PAT by its token id."""
        self.request("DELETE", f"/management/v1/users/{user_id}/pats/{token_id}")

    # ── Project roles + user grants (Zitadel's analog to Authentik groups) ─
    # Zitadel has no flat "groups": authorization is modeled as project ROLES
    # (defined on a project) plus user GRANTS (a user granted a set of role
    # keys on a project). `ensure-role` + `grant-user-role` are the closest
    # faithful mapping of Authentik's ensure-group + add-user-to-group.

    def list_roles(self, project_id: str) -> list[dict[str, Any]]:
        data = self.request(
            "POST",
            f"/management/v1/projects/{project_id}/roles/_search",
            body={"queries": []},
        )
        return (data or {}).get("result", []) or []

    def ensure_role(
        self,
        project_id: str,
        role_key: str,
        *,
        display_name: str | None = None,
        group: str = "",
    ) -> dict[str, Any]:
        """Create a project role (idempotent on ``roleKey``)."""
        for role in self.list_roles(project_id):
            if role.get("key") == role_key:
                return role
        return self.request(
            "POST",
            f"/management/v1/projects/{project_id}/roles",
            body={
                "roleKey": role_key,
                "displayName": display_name or role_key,
                "group": group,
            },
        )

    def find_user_grant(self, user_id: str, project_id: str) -> dict[str, Any] | None:
        data = self.request(
            "POST",
            "/management/v1/users/grants/_search",
            body={
                "queries": [
                    {"userIdQuery": {"userId": user_id}},
                    {"projectIdQuery": {"projectId": project_id}},
                ]
            },
        )
        for grant in (data or {}).get("result", []) or []:
            if grant.get("userId") == user_id and grant.get("projectId") == project_id:
                return grant
        return None

    def grant_user_role(
        self, project_id: str, user_id: str, role_keys: list[str]
    ) -> dict[str, Any]:
        """Grant project role(s) to a user (idempotent — updates an existing grant)."""
        existing = self.find_user_grant(user_id, project_id)
        if existing:
            grant_id = existing.get("id")
            self.request(
                "PUT",
                f"/management/v1/users/{user_id}/grants/{grant_id}",
                body={"roleKeys": role_keys},
            )
            return {"grantId": grant_id, "roleKeys": role_keys, "updated": True}
        return self.request(
            "POST",
            f"/management/v1/users/{user_id}/grants",
            body={"projectId": project_id, "roleKeys": role_keys},
        )

    def revoke_user_role(self, project_id: str, user_id: str) -> None:
        """Remove a user's grant for ``project_id`` (no-op if absent)."""
        existing = self.find_user_grant(user_id, project_id)
        if not existing:
            return
        self.request(
            "DELETE", f"/management/v1/users/{user_id}/grants/{existing.get('id')}"
        )
