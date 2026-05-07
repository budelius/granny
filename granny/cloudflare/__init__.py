"""Cloudflare provider — Workers / D1 / R2 / KV resource management.

Separate from ``granny.dns.cloudflare`` which handles only zone + DNS records.
This package covers the *account-level* resources that Workers-based apps
depend on.

All resources share a single ``CloudflareAccountClient`` whose token comes
from the granny credential chain (``CLOUDFLARE_API_TOKEN`` via vault/env).
The token must have at least the following scopes for a full Workers deploy:

- Account → Workers Scripts → Edit
- Account → D1 → Edit
- Account → Workers R2 Storage → Edit
- Account → Workers KV Storage → Edit
- Zone → Workers Routes → Edit (for the target zone)
- Zone → DNS → Edit (for custom-domain attach; also covered by granny.dns)
"""

from __future__ import annotations

import logging
import os

import requests

from granny.credentials.secrets import get_secret

logger = logging.getLogger(__name__)

API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareAccountClient:
    """Thin wrapper over the Cloudflare v4 REST API, scoped to one account."""

    def __init__(
        self,
        api_token: str | None = None,
        account_id: str | None = None,
    ) -> None:
        api_token = api_token or get_secret("CLOUDFLARE_API_TOKEN")
        if not api_token:
            raise ValueError("CLOUDFLARE_API_TOKEN not set (env or vault).")
        account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        if not account_id:
            # Try to resolve via /accounts (requires Account:List).
            resp = requests.get(
                f"{API_BASE}/accounts",
                headers={"Authorization": f"Bearer {api_token}"},
                timeout=15,
            )
            results = resp.json().get("result") or []
            if len(results) == 1:
                account_id = results[0]["id"]
            else:
                raise ValueError(
                    "CLOUDFLARE_ACCOUNT_ID not set and couldn't auto-resolve "
                    "(token lacks Account:List or multiple accounts visible). "
                    "Set CLOUDFLARE_ACCOUNT_ID explicitly."
                )
        self.account_id = account_id
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        raw_body: bytes | None = None,
        extra_headers: dict | None = None,
    ) -> dict:
        url = f"{API_BASE}{path}"
        headers = dict(self.session.headers)
        if extra_headers:
            headers.update(extra_headers)
        if raw_body is not None:
            # Caller is sending a non-JSON payload (e.g. multipart upload).
            headers.pop("Content-Type", None)
            resp = requests.request(
                method, url, headers=headers, params=params, data=raw_body, timeout=60
            )
        else:
            resp = self.session.request(
                method, url, params=params, json=json, timeout=60
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Cloudflare {method} {path} failed: {resp.status_code} {resp.text}"
            )
        body = resp.json()
        if not body.get("success", True):
            raise RuntimeError(
                f"Cloudflare {method} {path} error: {body.get('errors')}"
            )
        return body

    # -- Account scoping helper -----------------------------------------------

    def acct(self, subpath: str) -> str:
        """Build a path rooted at /accounts/<id>."""
        return f"/accounts/{self.account_id}{subpath}"
