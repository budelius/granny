"""Small Airtable Web API client used by the granny CLI."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote

import requests

from granny.credentials import get_secret


@dataclass
class AirtableClient:
    api_key: str
    api_base: str = "https://api.airtable.com/v0"
    meta_base: str = "https://api.airtable.com/v0/meta"

    @classmethod
    def from_environment(cls) -> "AirtableClient":
        api_key = get_secret("AIRTABLE_API_KEY")
        if not api_key:
            raise RuntimeError("AIRTABLE_API_KEY is not configured")
        return cls(api_key=api_key)

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Authorization", f"Bearer {self.api_key}")
        if method.upper() in {"POST", "PATCH", "PUT"}:
            headers.setdefault("Content-Type", "application/json")
        resp = requests.request(method, url, headers=headers, timeout=60, **kwargs)
        if not resp.ok:
            raise RuntimeError(f"Airtable API error {resp.status_code}: {resp.text[:1000]}")
        if not resp.content:
            return {}
        return resp.json()

    def list_bases(self) -> list[dict[str, Any]]:
        return list(self._request("GET", f"{self.meta_base}/bases").get("bases", []))

    def list_tables(self, base_id: str) -> list[dict[str, Any]]:
        return list(self._request("GET", f"{self.meta_base}/bases/{base_id}/tables").get("tables", []))

    def iter_records(
        self,
        base_id: str,
        table: str,
        *,
        fields: Iterable[str] | None = None,
        page_size: int = 100,
    ):
        table_path = quote(table, safe="")
        url = f"{self.api_base}/{base_id}/{table_path}"
        params: list[tuple[str, str | int]] = [("pageSize", page_size)]
        for field in fields or ():
            params.append(("fields[]", field))
        while True:
            data = self._request("GET", url, params=params)
            yield from data.get("records", [])
            offset = data.get("offset")
            if not offset:
                break
            params = [(k, v) for k, v in params if k != "offset"] + [("offset", offset)]

    def update_record(self, base_id: str, table: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        table_path = quote(table, safe="")
        return self._request(
            "PATCH",
            f"{self.api_base}/{base_id}/{table_path}/{record_id}",
            json={"fields": fields},
        )


def field_contains(value: Any, needle: str) -> bool:
    return needle in _stringify(value)


def replace_in_value(value: Any, old: str, new: str) -> tuple[Any, bool]:
    """Return value with string occurrences replaced, preserving container shape."""
    if isinstance(value, str):
        replaced = value.replace(old, new)
        return replaced, replaced != value
    if isinstance(value, list):
        changed = False
        out = []
        for item in value:
            replaced, item_changed = replace_in_value(item, old, new)
            out.append(replaced)
            changed = changed or item_changed
        return out, changed
    if isinstance(value, dict):
        changed = False
        out = {}
        for key, item in value.items():
            replaced, item_changed = replace_in_value(item, old, new)
            out[key] = replaced
            changed = changed or item_changed
        return out, changed
    return value, False


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(f"{key}: {_stringify(item)}" for key, item in value.items())
    return str(value)
