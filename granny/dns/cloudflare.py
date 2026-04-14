"""Cloudflare DNS provider — full CRUD against the v4 REST API.

Ported from ``tools/create/manage-dns.sh`` (the bash script this package
replaces) and the partial Cloudflare adapters in ``setup_bunny_storage.py``
and ``auto_certificate.py``. Uses ``requests`` directly so we don't depend
on the ``cloudflare`` SDK.
"""

import logging
from typing import Any

import requests

from granny.credentials.secrets import get_secret
from granny.dns.base import DNSProvider
from granny.dns.records import DNSRecord

logger = logging.getLogger(__name__)

API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareDNSProvider(DNSProvider):
    name = "cloudflare"

    def __init__(self, api_token: str | None = None) -> None:
        api_token = api_token or get_secret("CLOUDFLARE_API_TOKEN")
        if not api_token:
            raise ValueError("CLOUDFLARE_API_TOKEN not set (env or vault).")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            }
        )
        self._zone_cache: dict[str, str] = {}

    def _request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> dict:
        url = f"{API_BASE}{path}"
        resp = getattr(self.session, method.lower())(
            url, params=params, json=json, timeout=30
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Cloudflare {method} {path} failed: {resp.status_code} {resp.text}"
            )
        body = resp.json()
        if not body.get("success", True):
            errors = body.get("errors", [])
            raise RuntimeError(f"Cloudflare {method} {path} error: {errors}")
        return body

    def get_zone_id(self, zone_name: str) -> str:
        if zone_name in self._zone_cache:
            return self._zone_cache[zone_name]
        body = self._request("GET", "/zones", params={"name": zone_name})
        result = body.get("result") or []
        if not result:
            raise RuntimeError(f"Cloudflare zone not found: {zone_name}")
        zone_id = result[0]["id"]
        self._zone_cache[zone_name] = zone_id
        return zone_id

    def get_nameservers(self, zone_name: str) -> list[str]:
        body = self._request("GET", "/zones", params={"name": zone_name})
        result = body.get("result") or []
        if not result:
            return []
        return list(result[0].get("name_servers") or [])

    def _fqdn(self, zone_id: str, name: str) -> str:
        # Cloudflare's record API uses full FQDN for `name`. The caller passes
        # us a short name relative to the zone, so we need the zone name to
        # build the full one. We reverse-lookup via /zones/{id} once.
        body = self._request("GET", f"/zones/{zone_id}")
        zone_name = body["result"]["name"]
        if not name or name == "@":
            return zone_name
        if name.endswith(f".{zone_name}") or name == zone_name:
            return name
        return f"{name}.{zone_name}"

    def list_records(
        self,
        zone_id: str,
        name: str | None = None,
        record_type: str | None = None,
    ) -> list[DNSRecord]:
        params: dict[str, Any] = {"per_page": 100}
        if record_type:
            params["type"] = record_type.upper()
        if name is not None:
            params["name"] = self._fqdn(zone_id, name)
        body = self._request("GET", f"/zones/{zone_id}/dns_records", params=params)
        out: list[DNSRecord] = []
        for rec in body.get("result") or []:
            out.append(
                DNSRecord(
                    name=rec.get("name", ""),
                    type=rec.get("type", ""),
                    value=rec.get("content", ""),
                    ttl=int(rec.get("ttl", 300)),
                    id=rec.get("id"),
                    fqdn=rec.get("name"),
                    provider_data=rec,
                )
            )
        return out

    def get_record(
        self, zone_id: str, name: str, record_type: str
    ) -> DNSRecord | None:
        # Cloudflare stores `name` as the full FQDN on every record, so we
        # must filter by FQDN not short name.
        fqdn = self._fqdn(zone_id, name)
        for rec in self.list_records(zone_id, record_type=record_type):
            if rec.name == fqdn:
                return rec
        return None

    def create_record(
        self,
        zone_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = 300,
        proxied: bool = False,
    ) -> DNSRecord:
        fqdn = self._fqdn(zone_id, name)
        payload = {
            "type": record_type.upper(),
            "name": fqdn,
            "content": value,
            # Cloudflare uses ttl=1 as the sentinel for "automatic". Anything
            # else must be >= 60. Match manage-dns.sh's behavior: if caller
            # passes ttl<=1, use automatic; otherwise send the value.
            "ttl": 1 if ttl <= 1 else ttl,
            "proxied": proxied,
        }
        body = self._request("POST", f"/zones/{zone_id}/dns_records", json=payload)
        rec = body.get("result") or {}
        logger.info("Cloudflare: created %s %s -> %s", record_type, fqdn, value)
        return DNSRecord(
            name=rec.get("name", fqdn),
            type=rec.get("type", record_type.upper()),
            value=rec.get("content", value),
            ttl=int(rec.get("ttl", ttl)),
            id=rec.get("id"),
            fqdn=rec.get("name", fqdn),
            provider_data=rec,
        )

    def update_record(
        self,
        zone_id: str,
        record_id: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = 300,
        proxied: bool = False,
    ) -> DNSRecord:
        fqdn = self._fqdn(zone_id, name)
        payload = {
            "type": record_type.upper(),
            "name": fqdn,
            "content": value,
            "ttl": 1 if ttl <= 1 else ttl,
            "proxied": proxied,
        }
        body = self._request(
            "PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=payload
        )
        rec = body.get("result") or {}
        logger.info("Cloudflare: updated %s %s -> %s", record_type, fqdn, value)
        return DNSRecord(
            name=rec.get("name", fqdn),
            type=rec.get("type", record_type.upper()),
            value=rec.get("content", value),
            ttl=int(rec.get("ttl", ttl)),
            id=rec.get("id", record_id),
            fqdn=rec.get("name", fqdn),
            provider_data=rec,
        )

    def delete_record(self, zone_id: str, record_id: str) -> None:
        self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
        logger.info("Cloudflare: deleted record %s", record_id)
