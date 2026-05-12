"""Thin wrapper around the Authentik REST API.

Uses ``requests`` (already a granny core dependency) so this module does
not require any optional extras.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class AuthentikError(RuntimeError):
    """Raised on non-2xx responses from the Authentik API."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


class AuthentikClient:
    """Authenticated client for the Authentik v3 REST API.

    Construct directly when you already have the credentials, or use
    :meth:`from_environment` to resolve via granny's normal env/vault
    pipeline.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
        )

    @classmethod
    def from_environment(cls) -> "AuthentikClient":
        """Build a client from env vars + Vaultwarden fallback.

        Resolution order for the token:

        1. ``AUTHENTIK_API_TOKEN`` (env / .env / .deploy.env)
        2. ``AK_TOKEN`` (env / .env) — alias used by the standalone
           ``pseekoo-1-setup/authentik/authentik-api.py`` script
        3. Vaultwarden via Locke at ``granny/infra/authentik-api-token``
           (only registered for AUTHENTIK_API_TOKEN; AK_TOKEN does not
           round-trip through vault)

        For the base URL, ``AUTHENTIK_URL`` wins; default
        ``https://auth.pseekoo.io``.
        """
        import os

        from granny.credentials import get_secret

        token = get_secret("AUTHENTIK_API_TOKEN") or os.environ.get("AK_TOKEN")
        if not token:
            raise AuthentikError(
                0,
                "AUTHENTIK_API_TOKEN (or AK_TOKEN) is not set. "
                "Either export it, add it to .env/.deploy.env, or push the "
                "token to Vaultwarden under granny/infra/authentik-api-token "
                "and install the [vault] extra.",
            )
        base_url = os.environ.get("AUTHENTIK_URL") or "https://auth.pseekoo.io"
        return cls(base_url=base_url, token=token)

    # ── HTTP plumbing ────────────────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute one API request and return the decoded JSON body.

        Returns ``None`` on 204 / empty bodies.
        """
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        resp = self._session.request(
            method=method.upper(),
            url=url,
            params=clean_params or None,
            json=body,
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            raise AuthentikError(resp.status_code, resp.text)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Walk an Authentik list endpoint, returning all pages concatenated."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            p = dict(params or {})
            p["page"] = page
            data = self.request("GET", path, params=p) or {}
            out.extend(data.get("results", []))
            if not data.get("pagination", {}).get("next"):
                break
            page += 1
        return out

    # ── High-level lookups ───────────────────────────────────────────────

    def find_flow_pk(self, slug: str) -> str:
        results = self.request("GET", "/api/v3/flows/instances/", params={"slug": slug}).get(
            "results", []
        )
        for f in results:
            if f["slug"] == slug:
                return f["pk"]
        raise AuthentikError(404, f"No flow with slug={slug!r}")

    def find_signing_key_pk(self, *, prefer_self_signed: bool = True) -> str:
        """Return the pk of a key pair that can sign tokens."""
        keys = self.paginate("/api/v3/crypto/certificatekeypairs/")
        candidates = [k for k in keys if k.get("private_key_available")]
        if not candidates:
            raise AuthentikError(404, "No usable signing key pairs found")
        if prefer_self_signed:
            for k in candidates:
                if "self-signed" in k["name"].lower():
                    return k["pk"]
        return candidates[0]["pk"]

    def find_scope_mappings(self, scope_names: tuple[str, ...] | list[str]) -> list[str]:
        """Resolve scope-property-mapping pks by ``scope_name``."""
        mappings = self.paginate("/api/v3/propertymappings/provider/scope/")
        by_scope = {m["scope_name"]: m["pk"] for m in mappings}
        try:
            return [by_scope[scope] for scope in scope_names]
        except KeyError as exc:
            raise AuthentikError(404, f"Scope mapping for {exc.args[0]!r} not found") from exc

    def find_group_by_name(self, name: str) -> dict[str, Any] | None:
        for g in self.request("GET", "/api/v3/core/groups/", params={"name": name}).get(
            "results", []
        ):
            if g["name"] == name:
                return g
        return None

    def find_user_by_username(self, username: str) -> dict[str, Any] | None:
        for u in self.request("GET", "/api/v3/core/users/", params={"username": username}).get(
            "results", []
        ):
            if u["username"] == username:
                return u
        return None

    def find_provider_by_name(self, name: str) -> dict[str, Any] | None:
        for p in self.request("GET", "/api/v3/providers/oauth2/", params={"name": name}).get(
            "results", []
        ):
            if p["name"] == name:
                return p
        return None

    def find_application_by_slug(self, slug: str) -> dict[str, Any] | None:
        for a in self.request("GET", "/api/v3/core/applications/", params={"slug": slug}).get(
            "results", []
        ):
            if a["slug"] == slug:
                return a
        return None

    # ── Mutating helpers ─────────────────────────────────────────────────

    def ensure_group(self, name: str) -> dict[str, Any]:
        """Create a group if missing; return the (existing or new) group dict."""
        existing = self.find_group_by_name(name)
        if existing:
            return existing
        return self.request("POST", "/api/v3/core/groups/", body={"name": name})

    def add_user_to_group(self, username: str, group_name: str) -> None:
        user = self.find_user_by_username(username)
        if not user:
            raise AuthentikError(404, f"User {username!r} not found")
        group = self.find_group_by_name(group_name)
        if not group:
            raise AuthentikError(
                404, f"Group {group_name!r} not found — create it with ensure_group first"
            )
        self.request(
            "POST",
            f"/api/v3/core/groups/{group['pk']}/add_user/",
            body={"pk": user["pk"]},
        )

    def remove_user_from_group(self, username: str, group_name: str) -> None:
        user = self.find_user_by_username(username)
        if not user:
            raise AuthentikError(404, f"User {username!r} not found")
        group = self.find_group_by_name(group_name)
        if not group:
            raise AuthentikError(404, f"Group {group_name!r} not found")
        self.request(
            "POST",
            f"/api/v3/core/groups/{group['pk']}/remove_user/",
            body={"pk": user["pk"]},
        )

    def rotate_client_secret(self, provider_name: str) -> dict[str, Any]:
        """Rotate ``client_secret`` on an existing OAuth2 provider.

        Authentik regenerates the secret server-side when we PATCH it to
        an empty string. The returned dict carries the new value (when
        the API echoes it back — varies by version).
        """
        provider = self.find_provider_by_name(provider_name)
        if not provider:
            raise AuthentikError(404, f"Provider {provider_name!r} not found")
        return self.request(
            "PATCH",
            f"/api/v3/providers/oauth2/{provider['pk']}/",
            body={"client_secret": ""},
        )
