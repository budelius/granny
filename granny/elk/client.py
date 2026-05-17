"""Thin wrapper around Elasticsearch's security user API."""

from __future__ import annotations

import os
import secrets
import string
from typing import Any

import requests

from granny.credentials import get_secret


class ElasticsearchSecurityError(RuntimeError):
    """Raised on non-2xx responses from Elasticsearch security APIs."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


class ElasticsearchSecurityClient:
    """Authenticated client for Elasticsearch native-user management."""

    def __init__(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._session.verify = verify_tls
        if api_key:
            self._session.headers["Authorization"] = f"ApiKey {api_key}"
        elif username and password:
            self._session.auth = (username, password)
        else:
            raise ElasticsearchSecurityError(
                0,
                "Set ELASTICSEARCH_API_KEY or ELASTICSEARCH_USERNAME + "
                "ELASTICSEARCH_PASSWORD.",
            )

    @classmethod
    def from_environment(
        cls,
        *,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        verify_tls: bool | None = None,
    ) -> "ElasticsearchSecurityClient":
        """Build a client from explicit arguments, env vars, and vault fallback."""
        resolved_url = base_url or os.environ.get("ELASTICSEARCH_URL")
        if not resolved_url:
            raise ElasticsearchSecurityError(0, "ELASTICSEARCH_URL is not set")

        resolved_api_key = api_key or get_secret("ELASTICSEARCH_API_KEY")
        resolved_username = None
        resolved_password = None
        if not resolved_api_key:
            resolved_username = username or get_secret("ELASTICSEARCH_USERNAME")
            resolved_password = password or get_secret("ELASTICSEARCH_PASSWORD")
        if verify_tls is None:
            verify_tls = os.environ.get("ELASTICSEARCH_VERIFY_TLS", "true").lower() not in {
                "0",
                "false",
                "no",
            }
        return cls(
            resolved_url,
            username=resolved_username,
            password=resolved_password,
            api_key=resolved_api_key,
            verify_tls=verify_tls,
        )

    def request(self, method: str, path: str, *, body: Any = None) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        try:
            resp = self._session.request(
                method.upper(),
                self.base_url + path,
                json=body,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise ElasticsearchSecurityError(0, str(exc)) from exc
        if resp.status_code >= 400:
            raise ElasticsearchSecurityError(resp.status_code, resp.text)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def get_user(self, username: str) -> dict[str, Any] | None:
        try:
            result = self.request("GET", f"/_security/user/{username}")
        except ElasticsearchSecurityError as exc:
            if exc.status == 404:
                return None
            raise
        if isinstance(result, dict):
            return result.get(username)
        return None

    def put_user(
        self,
        username: str,
        *,
        roles: list[str] | tuple[str, ...],
        password: str | None = None,
        full_name: str | None = None,
        email: str | None = None,
        enabled: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a native Elasticsearch user."""
        body: dict[str, Any] = {
            "roles": list(roles),
            "enabled": enabled,
            "metadata": metadata or {},
        }
        if password is not None:
            body["password"] = password
        if full_name is not None:
            body["full_name"] = full_name
        if email is not None:
            body["email"] = email
        return self.request("PUT", f"/_security/user/{username}", body=body)


def generate_password(length: int = 24) -> str:
    """Generate a Kibana-friendly random password."""
    alphabet = string.ascii_letters + string.digits + "-_.!~"
    return "".join(secrets.choice(alphabet) for _ in range(length))
