"""Cloudflare D1 — create, list, execute SQL."""

from __future__ import annotations

from typing import Any

from granny.cloudflare import CloudflareAccountClient


class D1Client:
    def __init__(self, client: CloudflareAccountClient | None = None) -> None:
        self.c = client or CloudflareAccountClient()

    def list_databases(self) -> list[dict[str, Any]]:
        body = self.c._request("GET", self.c.acct("/d1/database"))
        return body.get("result") or []

    def get_database(self, name: str) -> dict[str, Any] | None:
        for db in self.list_databases():
            if db.get("name") == name:
                return db
        return None

    def create_database(self, name: str, primary_location_hint: str | None = None) -> dict[str, Any]:
        """Create a D1 database. Returns the full DB record including `uuid`."""
        existing = self.get_database(name)
        if existing:
            return existing
        payload: dict[str, Any] = {"name": name}
        if primary_location_hint:
            payload["primary_location_hint"] = primary_location_hint
        body = self.c._request("POST", self.c.acct("/d1/database"), json=payload)
        return body["result"]

    def execute_sql(self, database_id: str, sql: str) -> list[dict[str, Any]]:
        """Execute one or more SQL statements against a D1 database."""
        body = self.c._request(
            "POST",
            self.c.acct(f"/d1/database/{database_id}/query"),
            json={"sql": sql},
        )
        return body.get("result") or []
