"""Requests client for a Knative FaaS control API.

Covers the HTTP operations exposed by the ``/api/faas/*`` routes:

  deploy  → POST   /api/faas/functions
  list    → GET    /api/faas/functions
  get     → GET    /api/faas/functions/{name}
  scale   → PATCH  /api/faas/functions/{name}
  delete  → DELETE /api/faas/functions/{name}
  logs    → GET    /api/faas/functions/{name}/logs?tail={n}
  invoke  → POST   /api/faas/functions/{name}/invoke

Auth uses the ``X-API-Key`` header. Configuration resolves through the
granny credentials chain (environment, then Vaultwarden).

The API key is never included in exceptions or log output.
"""

from __future__ import annotations

import logging
import re

import requests

from granny.credentials.secrets import get_secret

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")


class KnativeNotFoundError(KeyError, RuntimeError):
    """Raised when a function does not exist."""


class KnativeClient:
    """Authenticated HTTP client for a Knative FaaS control API.

    Parameters
    ----------
    base_url:
        Base URL of the control plane (e.g. ``https://faas-api.example.com``).
        Resolved from ``OBACHAN_API_BASE_URL`` (env or vault) when not passed.
        Trailing slash is stripped.
    api_key:
        APISIX ``X-API-Key`` value.  Resolved from ``OBACHAN_API_KEY`` (env
        or vault) when not passed.

    Raises
    ------
    ValueError
        If ``base_url`` or ``api_key`` cannot be resolved to a non-empty string.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.base_url = (base_url or get_secret("OBACHAN_API_BASE_URL") or "").rstrip(
            "/"
        )
        api_key = api_key or get_secret("OBACHAN_API_KEY")

        if not self.base_url:
            raise ValueError(
                "OBACHAN_API_BASE_URL not set (env or vault). "
                "Pass base_url= or export OBACHAN_API_BASE_URL=https://faas-api.<zone>"
            )
        if not api_key:
            raise ValueError(
                "OBACHAN_API_KEY not set (env or vault). "
                "Pass api_key= or export OBACHAN_API_KEY=<key>"
            )

        self._session = requests.Session()
        self._session.headers.update(
            {"X-API-Key": api_key, "Content-Type": "application/json"}
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_name(self, name: str) -> None:
        """Raise ValueError for invalid function names before any HTTP call.

        Enforces ``^[a-z][a-z0-9-]{0,61}[a-z0-9]$``: lowercase letters,
        digits, and hyphens; must start and end with a letter or digit;
        maximum 63 characters.
        """
        if not name or not _NAME_RE.match(name):
            raise ValueError(
                f"Invalid function name {name!r}: must match "
                r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$ "
                "(lowercase letters, digits, hyphens; start/end with letter or digit; "
                "max 63 chars)"
            )

    def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        """Execute one API request and return the decoded JSON body.

        Returns an empty dict for 204 / empty responses.

        Raises
        ------
        RuntimeError
            On any non-2xx status.  The message contains the HTTP method,
            path, and status code.  The ``X-API-Key`` value is never included.
        """
        url = f"{self.base_url}{path}"
        resp = getattr(self._session, method.lower())(url, json=data, timeout=30)
        if resp.status_code >= 400:
            message = f"FaaS API {method} {path}: {resp.status_code} {resp.text[:1000]}"
            if resp.status_code == 404:
                raise KnativeNotFoundError(message)
            raise RuntimeError(message)
        if resp.status_code == 204 or not resp.text:
            return {}
        return resp.json()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def deploy(
        self,
        name: str,
        image: str,
        *,
        env_vars: dict[str, str] | None = None,
        min_scale: int = 0,
        max_scale: int = 5,
    ) -> dict:
        """Deploy (create or update) a Knative service.

        Sends ``POST /api/faas/functions`` with the full service spec.

        Parameters
        ----------
        name:
            Function name (``^[a-z][a-z0-9-]{0,61}[a-z0-9]$``).
        image:
            Container image reference (e.g. ``registry.example.io/fn:latest``).
        env_vars:
            Environment variables injected into the service container.
            Defaults to ``{}`` when not provided.
        min_scale:
            Minimum replica count (Knative ``autoscaling.knative.dev/min-scale``).
        max_scale:
            Maximum replica count (Knative ``autoscaling.knative.dev/max-scale``).

        Returns
        -------
        dict
            The ksvc dict returned by the server on 201.
        """
        self._validate_name(name)
        return self._request(
            "POST",
            "/api/faas/functions",
            {
                "name": name,
                "image": image,
                "env_vars": env_vars or {},
                "min_scale": min_scale,
                "max_scale": max_scale,
            },
        )

    def list(self) -> list[dict]:
        """Return the list of deployed functions.

        Sends ``GET /api/faas/functions`` and extracts the ``items`` key.

        Returns
        -------
        list[dict]
            List of ksvc dicts, or ``[]`` when no functions are deployed.
        """
        return self._request("GET", "/api/faas/functions").get("items", [])

    def get(self, name: str) -> dict | None:
        """Fetch a single function by name.

        Sends ``GET /api/faas/functions/{name}``.

        Returns
        -------
        dict
            The ksvc dict on 200.
        None
            When the server responds 404.

        Raises
        ------
        RuntimeError
            On any non-2xx response other than 404.
        """
        self._validate_name(name)
        try:
            return self._request("GET", f"/api/faas/functions/{name}")
        except KeyError:
            return None

    def scale(
        self,
        name: str,
        *,
        min_scale: int | None = None,
        max_scale: int | None = None,
    ) -> dict:
        """Update the scale bounds of an existing function.

        Sends ``PATCH /api/faas/functions/{name}`` with only the scale
        fields that are explicitly provided (``None`` means "don't change").

        Parameters
        ----------
        name:
            Function name.
        min_scale:
            New minimum replica count, or ``None`` to leave unchanged.
        max_scale:
            New maximum replica count, or ``None`` to leave unchanged.

        Returns
        -------
        dict
            ``{"scaled": true}`` on 200.
        """
        self._validate_name(name)
        body: dict[str, int] = {}
        if min_scale is not None:
            body["min_scale"] = min_scale
        if max_scale is not None:
            body["max_scale"] = max_scale
        return self._request("PATCH", f"/api/faas/functions/{name}", body)

    def delete(self, name: str) -> None:
        """Delete a deployed function.

        Sends ``DELETE /api/faas/functions/{name}``.  Returns nothing on 204.

        Raises
        ------
        KeyError
            When the function does not exist.
        RuntimeError
            On any other non-2xx response.
        """
        self._validate_name(name)
        self._request("DELETE", f"/api/faas/functions/{name}")

    def logs(self, name: str, tail: int = 50) -> str:
        """Retrieve recent log lines for a function.

        Sends ``GET /api/faas/functions/{name}/logs?tail={tail}``.
        Bypasses ``_request`` to return the raw ``text/plain`` body rather
        than attempting JSON decoding.

        Parameters
        ----------
        name:
            Function name.
        tail:
            Number of log lines to return (passed as query param).

        Returns
        -------
        str
            Raw log text from the server.

        Raises
        ------
        RuntimeError
            On any non-2xx response.
        """
        self._validate_name(name)
        url = f"{self.base_url}/api/faas/functions/{name}/logs"
        resp = self._session.get(url, params={"tail": tail}, timeout=30)
        if resp.status_code >= 400:
            message = f"FaaS API logs {name}: {resp.status_code} {resp.text[:1000]}"
            if resp.status_code == 404:
                raise KnativeNotFoundError(message)
            raise RuntimeError(message)
        return resp.text

    def invoke(
        self,
        name: str,
        *,
        method: str = "GET",
        path: str = "/",
        body: bytes | None = None,
    ) -> bytes:
        """Invoke a deployed function via the control-plane proxy.

        Sends ``POST /api/faas/functions/{name}/invoke`` and returns the
        raw response bytes.  Bypasses ``_request`` because the response is
        ``application/octet-stream``, not JSON.

        Parameters
        ----------
        name:
            Function name.
        method:
            HTTP method to forward to the function (default ``"GET"``).
        path:
            Request path to forward (default ``"/"``).
        body:
            Optional request body bytes.  Decoded to UTF-8 (errors replaced)
            before JSON-serialisation into the invoke payload.

        Returns
        -------
        bytes
            Raw bytes from the function's response.

        Raises
        ------
        RuntimeError
            On any non-2xx response.
        """
        self._validate_name(name)
        url = f"{self.base_url}/api/faas/functions/{name}/invoke"
        payload_body = body.decode("utf-8", errors="replace") if body else None
        resp = self._session.post(
            url,
            json={"method": method, "path": path, "body": payload_body},
            timeout=30,
        )
        if resp.status_code >= 400:
            message = f"FaaS API invoke {name}: {resp.status_code} {resp.text[:1000]}"
            if resp.status_code == 404:
                raise KnativeNotFoundError(message)
            raise RuntimeError(message)
        return resp.content
