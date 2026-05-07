"""Cloudflare Workers — script metadata + secrets management.

Deploying the actual Worker bundle is left to ``wrangler deploy`` which the
user already runs from their project directory. Granny's job here is to
manage the surrounding infrastructure: secrets, custom domains, D1/R2/KV
bindings that wrangler's config file references.
"""

from __future__ import annotations

from typing import Any

from granny.cloudflare import CloudflareAccountClient


class WorkersClient:
    def __init__(self, client: CloudflareAccountClient | None = None) -> None:
        self.c = client or CloudflareAccountClient()

    def list_scripts(self) -> list[dict[str, Any]]:
        body = self.c._request("GET", self.c.acct("/workers/scripts"))
        return body.get("result") or []

    def put_secret(self, script_name: str, name: str, value: str) -> dict[str, Any]:
        """Create or update a plaintext secret binding on a Worker."""
        body = self.c._request(
            "PUT",
            self.c.acct(f"/workers/scripts/{script_name}/secrets"),
            json={"name": name, "text": value, "type": "secret_text"},
        )
        return body.get("result") or {}

    def delete_secret(self, script_name: str, name: str) -> None:
        self.c._request(
            "DELETE",
            self.c.acct(f"/workers/scripts/{script_name}/secrets/{name}"),
        )

    def list_secrets(self, script_name: str) -> list[dict[str, Any]]:
        """Returns metadata only (names + types), not the plaintext values."""
        body = self.c._request(
            "GET", self.c.acct(f"/workers/scripts/{script_name}/secrets")
        )
        return body.get("result") or []
