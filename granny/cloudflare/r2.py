"""Cloudflare R2 — bucket management."""

from __future__ import annotations

from typing import Any

from granny.cloudflare import CloudflareAccountClient


class R2Client:
    def __init__(self, client: CloudflareAccountClient | None = None) -> None:
        self.c = client or CloudflareAccountClient()

    def list_buckets(self) -> list[dict[str, Any]]:
        body = self.c._request("GET", self.c.acct("/r2/buckets"))
        return body.get("result", {}).get("buckets") or []

    def get_bucket(self, name: str) -> dict[str, Any] | None:
        for b in self.list_buckets():
            if b.get("name") == name:
                return b
        return None

    def create_bucket(self, name: str, location: str | None = None) -> dict[str, Any]:
        """Idempotent bucket create — returns the bucket record."""
        existing = self.get_bucket(name)
        if existing:
            return existing
        payload: dict[str, Any] = {"name": name}
        if location:
            payload["locationHint"] = location
        body = self.c._request("POST", self.c.acct("/r2/buckets"), json=payload)
        return body.get("result") or {"name": name}
